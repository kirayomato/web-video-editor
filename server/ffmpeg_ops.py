"""ffmpeg/ffprobe 封装：探测、命令构建、执行、时间线素材生成。零第三方依赖。"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "output"
UPLOAD_DIR = ROOT / "tmp" / "uploads"
CACHE_DIR = ROOT / "tmp" / "cache"
for _d in (OUT_DIR, UPLOAD_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

FFMPEG = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe") or "ffprobe"

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0


def _popen_flags():
    return {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}


# ---------- 进程并发控制 ----------
# 同一时刻只允许一个 ffmpeg/ffprobe 子进程：并发会互相抢 CPU，
# 转码/波形这类吃满多核的命令叠加后总耗时反而更长。
# 可重入：一次请求内会连续调用多条命令（探测→缓存→波形），
# 每条命令各自持锁、结束即释放，不跨调用持有。
_PROC_LOCK = threading.RLock()
# 拿不到锁的最大等待秒数，超时按"忙"处理，避免请求无限挂起
PROC_WAIT = float(os.environ.get("FFMPEG_WAIT") or 10)


class FfmpegBusy(RuntimeError):
    """等待 ffmpeg 空闲超时。"""


@contextmanager
def proc_slot(timeout=None):
    """独占 ffmpeg 子进程槽位；timeout 为 None 表示一直等。"""
    if not _PROC_LOCK.acquire(timeout=-1 if timeout is None else timeout):
        raise FfmpegBusy("ffmpeg 正忙（另有任务在执行），请稍后重试")
    try:
        yield
    finally:
        _PROC_LOCK.release()


# 同一文件同一操作的并发调用合并为一次执行，其余调用共享返回值/异常。
# 快速切换文件或反复拖动入点时，上一轮波形还没算完，新请求直接等结果。
_flight_lock = threading.Lock()
_flights: dict[str, dict] = {}


def single_flight(key: str, fn):
    with _flight_lock:
        slot = _flights.get(key)
        owner = slot is None
        if owner:
            slot = {"event": threading.Event(), "value": None, "error": None}
            _flights[key] = slot
    if not owner:
        slot["event"].wait()
        if slot["error"] is not None:
            raise slot["error"]
        return slot["value"]
    try:
        slot["value"] = fn()
        return slot["value"]
    except BaseException as e:
        slot["error"] = e
        raise
    finally:
        with _flight_lock:
            _flights.pop(key, None)
        slot["event"].set()


def _file_key(tag: str, file_path: str, extra: str = "") -> str:
    """缓存/合并键：tag + 路径 + 大小 + 修改时间，文件被覆盖后自然失效。"""
    try:
        st = os.stat(file_path)
        stamp = f"{st.st_size}|{st.st_mtime}"
    except OSError:
        stamp = "-"
    return f"{tag}|{file_path}|{stamp}|{extra}"


def _coalesce(tag: str):
    """把 file_path 相同的并发调用合并成一次执行。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(file_path, *args, **kwargs):
            extra = f"{args!r}|{sorted(kwargs.items())!r}"
            key = _file_key(tag, file_path, extra)
            return single_flight(key, lambda: fn(file_path, *args, **kwargs))

        return wrapper

    return deco


_QUOTE_CHARS = "\"'“”‘’«»"


def clean_path_str(p) -> str:
    """清洗用户粘贴的路径：去空白/BOM/成对引号。

    Windows 资源管理器「复制为路径」会带一对双引号，直接当相对路径
    拼上工作目录会产生含引号的非法路径（Invalid argument）。
    """
    if p is None:
        return ""
    s = str(p).strip().lstrip("\ufeff").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in _QUOTE_CHARS:
        s = s[1:-1].strip()
    # 兜底：残缺/不配对的引号（Windows 文件名不允许包含 "，首尾 ' 也极罕见）
    s = s.strip(_QUOTE_CHARS)
    return s


def assert_local_path(p, *, must_exist: bool = True) -> str:
    """清洗并规范化为绝对路径；默认要求文件存在，错误信息可读。"""
    s = clean_path_str(p)
    if not s:
        raise ValueError("缺少路径参数")
    if "\x00" in s or '"' in s:
        raise ValueError("路径含非法字符")
    s = os.path.normpath(os.path.abspath(s))
    if must_exist and not os.path.isfile(s):
        raise ValueError(f"文件不存在：{s}")
    return s


def assert_local_dir(p) -> str:
    """清洗并规范化为绝对路径，要求目录存在（文件浏览器用）。"""
    s = clean_path_str(p)
    if not s:
        raise ValueError("缺少路径参数")
    if "\x00" in s or '"' in s:
        raise ValueError("路径含非法字符")
    s = os.path.normpath(os.path.abspath(s))
    if not os.path.isdir(s):
        raise ValueError(f"目录不存在：{s}")
    return s


def parse_fps(rate):
    if not rate or rate == "0/0":
        return None
    m = re.match(r"^(\d+)/(\d+)$", str(rate))
    if m:
        den = int(m.group(2))
        return int(m.group(1)) / den if den else None
    try:
        n = float(rate)
        return n if n > 0 else None
    except ValueError:
        return None


def parse_time(value) -> float:
    if value is None or value == "":
        return 0.0
    m = re.match(r"^-?(\d+):(\d+):(\d+(?:\.\d+)?)$", str(value).strip())
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    try:
        return float(value)
    except ValueError:
        return 0.0


def ff_time(sec: float) -> str:
    s = max(0.0, float(sec or 0))
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    part = s % 60
    return f"{h:02d}:{m:02d}:{part:06.3f}"


def run_collect(args, max_buffer: int = 32 * 1024 * 1024) -> str:
    """执行 ffmpeg 命令并收集 stdout（短命令）。"""
    with proc_slot(PROC_WAIT):
        return _run_collect(args, max_buffer)


class AudioPrepareCancelled(RuntimeError):
    """抽音轨被终止。"""


def _out_progress(out_time_us: float, duration: float) -> float | None:
    """out_time（微秒）换算进度比例；duration 非正时返回 None。

    ffmpeg 的 out_time_ms 名为毫秒实为微秒（历史 bug，值与 out_time_us 相同），
    两个字段统一按微秒换算；按毫秒换算会让进度瞬间打满 0.99。
    """
    if duration <= 0:
        return None
    return min(0.99, out_time_us / (duration * 1e6))


def run_progress(args, duration: float, slot: dict) -> None:
    """执行长命令并把进度写进 slot；slot["cancel"] 置位时终止子进程。

    stderr 由独立线程排空，避免管道写满后子进程卡在日志输出上。
    """
    with proc_slot(PROC_WAIT):
        if slot.get("cancel"):
            raise AudioPrepareCancelled("已终止")
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_popen_flags(),
        )
        slot["proc"] = proc
        err_lines: list[str] = []

        def drain_stderr():
            for line in proc.stderr:
                err_lines.append(line.decode("utf-8", "replace").strip())

        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()
        cancelled = False
        for raw in proc.stdout:
            if slot.get("cancel"):
                proc.kill()
                cancelled = True
                break
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k in ("out_time_us", "out_time_ms"):
                try:
                    t = float(v)
                except ValueError:
                    continue
                p = _out_progress(t, duration)
                if p is not None:
                    slot["progress"] = max(slot["progress"], p)
            elif k == "progress" and v == "end":
                slot["progress"] = 1.0
        proc.wait()
        reader.join(timeout=2)
        slot.pop("proc", None)
        if cancelled or slot.get("cancel"):
            raise AudioPrepareCancelled("已终止")
        if proc.returncode != 0:
            raise RuntimeError(
                "\n".join(err_lines[-6:]) or f"ffmpeg 退出码 {proc.returncode}"
            )
        slot["progress"] = 1.0


# 只读探测独立排队：转码期间仍要能读元数据，但自身串行，
# 快速切换文件时不会同时拉起一堆 ffprobe。
_PROBE_LOCK = threading.RLock()


def run_probe(args, max_buffer: int = 32 * 1024 * 1024) -> str:
    """执行只读探测命令（ffprobe / ffmpeg -version），不占 ffmpeg 槽位。"""
    if not _PROBE_LOCK.acquire(timeout=PROC_WAIT):
        raise FfmpegBusy("探测队列繁忙，请稍后重试")
    try:
        return _run_collect(args, max_buffer)
    finally:
        _PROBE_LOCK.release()


def _run_collect(args, max_buffer: int) -> str:
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_popen_flags(),
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(
            "\n".join(err.splitlines()[-6:])
            or f"{' '.join(args[:2])} 退出码 {proc.returncode}"
        )
    out = proc.stdout[:max_buffer].decode("utf-8", "replace")
    return out


@_coalesce("probe")
def probe(file_path: str) -> dict:
    raw = run_probe(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            file_path,
        ]
    )
    data = json.loads(raw or "{}")
    fmt = data.get("format") or {}
    streams = []
    for s in data.get("streams") or []:
        base = {
            "index": s.get("index"),
            "codec_type": s.get("codec_type"),
            "codec_name": s.get("codec_name"),
            "bitrate": int(s["bit_rate"]) if s.get("bit_rate") else None,
            "language": (s.get("tags") or {}).get("language"),
        }
        if s.get("codec_type") == "video":
            base.update(
                {
                    "width": s.get("width"),
                    "height": s.get("height"),
                    "fps": parse_fps(s.get("avg_frame_rate"))
                    or parse_fps(s.get("r_frame_rate")),
                    "pix_fmt": s.get("pix_fmt"),
                }
            )
        if s.get("codec_type") == "audio":
            base.update(
                {
                    "channels": s.get("channels"),
                    "sample_rate": (
                        int(s["sample_rate"]) if s.get("sample_rate") else None
                    ),
                }
            )
        streams.append(base)

    def _pick(t):
        return next((x for x in streams if x["codec_type"] == t), None)

    return {
        "path": file_path,
        "name": os.path.basename(file_path),
        "size": int(fmt["size"]) if fmt.get("size") else None,
        "duration": float(fmt["duration"]) if fmt.get("duration") else None,
        "format_name": fmt.get("format_name"),
        "bitrate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "streams": streams,
        "video": _pick("video"),
        "audio": _pick("audio"),
    }


@_coalesce("keyframes")
def keyframes_near(file_path: str, at: float, window: float = 5.0) -> list:
    start = max(0.0, float(at) - window)
    end = float(at) + window
    # read_intervals 绝对区间语法：START%END（均为绝对秒）；%+N 形式是相对偏移，勿混用
    interval = f"{start:.3f}%{end:.3f}"
    raw = run_probe(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time,pict_type",
            "-of",
            "csv=p=0",
            "-read_intervals",
            interval,
            file_path,
        ]
    )
    frames = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        try:
            t = float(parts[0])
        except ValueError:
            continue
        frames.append({"time": t, "type": parts[1] if len(parts) > 1 else None})
    frames.sort(key=lambda f: f["time"])
    return frames


def _safe_base(file_path: str) -> str:
    stem = Path(file_path).stem
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", stem)[:60]
    return cleaned or "video"


def _out_path(file_path: str, op: str, ext: str) -> str:
    stamp = time.strftime("%Y%m%d%H%M%S")
    return str(OUT_DIR / f"{_safe_base(file_path)}_{op}_{stamp}{ext}")


def _temp_file(prefix: str, ext: str) -> str:
    return str(CACHE_DIR / f"{prefix}_{os.urandom(6).hex()}{ext}")


_AUDIO_EXT = {
    "aac": ".m4a",
    "mp3": ".mp3",
    "opus": ".opus",
    "vorbis": ".ogg",
    "flac": ".flac",
    "ac3": ".ac3",
    "eac3": ".eac3",
    "wmav2": ".wma",
    "pcm_s16le": ".wav",
}


def _audio_ext(codec) -> str:
    """音频直拷时按源编码选择容器扩展名。"""
    return _AUDIO_EXT.get((codec or "").lower(), ".m4a")


def _concat_list(paths) -> str:
    file = _temp_file("concat", ".txt")
    lines = []
    for p in paths:
        norm = str(p).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{norm}'")
    Path(file).write_text("\n".join(lines), encoding="utf-8")
    return file


def build_command(task: dict) -> dict:
    """任务 → ffmpeg 命令步骤链。返回 {steps, outPath, command}。"""
    g = ["-y", "-hide_banner", "-nostdin"]
    steps: list[list[str]] = []
    out_path = None
    t = task.get("type")

    if t == "trim":
        start = float(task.get("start") or 0)
        end = float(task.get("end") or 0)
        if end <= start:
            raise ValueError("出点必须大于入点")
        dur = end - start
        if task.get("mode") == "precise":
            out_path = _out_path(task["path"], "trim_precise", ".mp4")
            # 输入侧粗跳（快速）+ 输出侧精修（精确）。
            # 不能只用输入侧 -ss：音频 copy 无法解码丢包，mov demuxer seek 会带
            # 约 1s 负时间戳前摇进输出（内容错位并污染 concat）；输出侧 -ss/-t
            # 按时间戳丢弃，保证音频 copy 也精确。
            pre = max(0.0, start - 2.0)
            steps.append(
                g
                + [
                    "-ss",
                    ff_time(pre),
                    "-i",
                    task["path"],
                    "-ss",
                    ff_time(start - pre),
                    "-t",
                    ff_time(dur),
                    "-c:v",
                    "libx264",
                    "-crf",
                    str(task.get("crf", 18)),
                    "-preset",
                    task.get("preset") or "veryfast",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    out_path,
                ]
            )
        else:
            out_path = _out_path(task["path"], "trim", ".mp4")
            steps.append(
                g
                + [
                    "-ss",
                    ff_time(start),
                    "-i",
                    task["path"],
                    "-t",
                    ff_time(dur),
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    out_path,
                ]
            )

    elif t == "cut-middle":
        in_p = float(task.get("in") or 0)
        out_p = float(task.get("out") or 0)
        raw_dur = task.get("duration")
        # 时长未知（读取失败）时用极大值占位：尾部保留到 EOF，ffmpeg 读到文件末尾
        duration = float(raw_dur) if raw_dur else 1e9
        segs = []
        if in_p > 0.05:
            segs.append((0.0, in_p))
        if out_p < duration - 0.05:
            segs.append((out_p, duration))
        if not segs:
            raise ValueError("删除区间覆盖了整个视频")
        seg_files = []
        for i, (s, e) in enumerate(segs):
            seg = _temp_file(f"seg{i}", ".mp4")
            if task.get("mode") == "precise":
                # 同 trim 精确：输入侧粗跳 + 输出侧精修，避免音频 copy 负时间戳前摇
                pre = max(0.0, s - 2.0)
                steps.append(
                    g
                    + [
                        "-ss",
                        ff_time(pre),
                        "-i",
                        task["path"],
                        "-ss",
                        ff_time(s - pre),
                        "-t",
                        ff_time(e - s),
                        "-c:v",
                        "libx264",
                        "-crf",
                        str(task.get("crf", 18)),
                        "-preset",
                        task.get("preset") or "veryfast",
                        "-c:a",
                        "copy",
                        seg,
                    ]
                )
            else:
                steps.append(
                    g
                    + [
                        "-ss",
                        ff_time(s),
                        "-i",
                        task["path"],
                        "-t",
                        ff_time(e - s),
                        "-c",
                        "copy",
                        "-avoid_negative_ts",
                        "make_zero",
                        seg,
                    ]
                )
            seg_files.append(seg)
        out_path = _out_path(task["path"], "cutmiddle", ".mp4")
        lst = _concat_list(seg_files)
        steps.append(
            g
            + [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                lst,
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                out_path,
            ]
        )

    elif t == "concat":
        paths = [str(p).strip() for p in (task.get("paths") or []) if str(p).strip()]
        if len(paths) < 2:
            raise ValueError("拼接至少需要 2 个文件")
        lst = _concat_list(paths)
        if task.get("mode") == "transcode":
            out_path = _out_path(paths[0], "concat_tc", ".mp4")
            # 视频统一转码，音频一律 stream copy
            steps.append(
                g
                + [
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    lst,
                    "-c:v",
                    "libx264",
                    "-crf",
                    str(task.get("crf", 20)),
                    "-preset",
                    task.get("preset") or "medium",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    out_path,
                ]
            )
        else:
            out_path = _out_path(paths[0], "concat", ".mp4")
            steps.append(
                g
                + [
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    lst,
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    out_path,
                ]
            )

    elif t == "audio":
        if task.get("action") == "extract":
            # 音频一律 stream copy，容器按源编码选择
            ext = _audio_ext(task.get("_audio_codec"))
            out_path = _out_path(task["path"], "audio", ext)
            steps.append(g + ["-i", task["path"], "-vn", "-c:a", "copy", out_path])
        else:
            out_path = _out_path(task["path"], "noaudio", ".mp4")
            steps.append(g + ["-i", task["path"], "-c:v", "copy", "-an", out_path])

    elif t == "frame":
        fmt = "png" if task.get("format") == "png" else "jpg"
        out_path = _out_path(task["path"], "frame", f".{fmt}")
        steps.append(
            g
            + [
                "-ss",
                ff_time(float(task.get("time") or 0)),
                "-i",
                task["path"],
                "-frames:v",
                "1",
                "-q:v",
                "2",
                out_path,
            ]
        )

    elif t == "transcode":
        fmt = "webm" if task.get("format") == "webm" else "mp4"
        out_path = _out_path(task["path"], "transcode", f".{fmt}")
        args = g + ["-i", task["path"]]
        if task.get("scale"):
            args += ["-vf", f"scale=-2:{int(task['scale'])}"]
        # 视频重编码，音频一律 stream copy
        if fmt == "webm":
            args += [
                "-c:v",
                "libvpx-vp9",
                "-crf",
                str(task.get("crf", 32)),
                "-b:v",
                "0",
                "-c:a",
                "copy",
            ]
        else:
            args += [
                "-c:v",
                "libx264",
                "-crf",
                str(task.get("crf", 23)),
                "-preset",
                task.get("preset") or "medium",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
            ]
        args.append(out_path)
        steps.append(args)

    else:
        raise ValueError(f"未知任务类型: {t}")

    command = " && ".join(_command_string(FFMPEG, s) for s in steps)
    return {"steps": steps, "outPath": out_path, "command": command}


def _quote(arg: str) -> str:
    s = str(arg)
    if re.search(r"[\s\"'&|<>^%]", s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _command_string(binary: str, args) -> str:
    return " ".join([_quote(binary)] + [_quote(a) for a in args])


class StepRunner:
    """顺序执行命令步骤链，支持进度回调与取消。"""

    def __init__(self, steps, duration=None, on_progress=None):
        self.steps = steps
        self.duration = duration
        self.on_progress = on_progress
        self.procs = []
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        for p in self.procs:
            try:
                p.kill()
            except Exception:
                pass

    def run(self):
        # 整个任务执行期间独占 ffmpeg 槽位（不设等待上限：任务已由队列调度，
        # 不应因其他短命令占用而被丢弃）。跨步骤也持有，避免探测/波形插入
        # 多步任务的中间态。
        with proc_slot(None):
            total = len(self.steps)
            for i, args in enumerate(self.steps):
                self._run_one(args, i, total, is_last=(i == total - 1))
            return {"ok": True}

    def _run_one(self, args, index, total, is_last):
        cmd = ["-progress", "pipe:1", "-nostats"] + args
        proc = subprocess.Popen(
            [FFMPEG] + cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_popen_flags(),
        )
        self.procs.append(proc)
        stderr_chunks = []
        out_us = None

        def read_stderr():
            for raw in iter(proc.stderr.readline, b""):
                stderr_chunks.append(raw)
                if sum(len(c) for c in stderr_chunks) > 512 * 1024:
                    del stderr_chunks[: len(stderr_chunks) // 2]

        import threading

        t_err = threading.Thread(target=read_stderr, daemon=True)
        t_err.start()

        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if key in ("out_time_us", "out_time_ms"):
                try:
                    out_us = float(val)
                except ValueError:
                    pass
            elif key == "out_time":
                out_us = parse_time(val) * 1e6
            elif key == "progress" and self.on_progress:
                out_time = (out_us or 0) / 1e6
                progress = min(1.0, out_time / self.duration) if self.duration else None
                self.on_progress(
                    {
                        "stepIndex": index,
                        "stepCount": total,
                        "progress": progress,
                        "outTime": out_time,
                    }
                )

        proc.stdout.close()
        code = proc.wait()
        t_err.join(timeout=2)
        if code != 0:
            if self._cancelled:
                raise RuntimeError("任务已取消")
            err = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
            raise RuntimeError(
                "\n".join(err.splitlines()[-8:]) or f"ffmpeg 退出码 {code}"
            )


# 时间线波形分辨率：宽度按时长等比例放大（每半小时 4000px），深度缩放浏览时
# 横向清晰度恒定（列宽只取决于可见窗口，不再随整段时长被摊薄）；高度固定，
# 供 CSS 纵向拉伸填满。
WAVE_HEIGHT = 600
WAVE_SECONDS_PER_IMAGE = 30 * 60  # 一幅图的基准时长（半小时）
WAVE_WIDTH_BASE = 4000  # 该时长对应的横向像素
WAVE_MIN_WIDTH = 2000  # 短视频保底宽度
WAVE_MAX_WIDTH = 20000  # 最大宽度


def _wave_width(duration: float) -> int:
    """按时长计算波形图横向像素（时长未知时回落基准宽度）。"""
    if not duration or duration <= 0:
        return WAVE_WIDTH_BASE
    return min(
        WAVE_MAX_WIDTH,
        max(
            WAVE_MIN_WIDTH,
            int(round(duration / WAVE_SECONDS_PER_IMAGE * WAVE_WIDTH_BASE)),
        ),
    )


def wave_gain_db(file_path: str) -> float:
    """波形显示用自动增益（dB）：把音频峰值拉到 0dBFS，安静录音自动放大。

    只影响波形可视化，不改动任何导出产物。静音/无音轨返回 0；
    增益上限 60dB，防止近乎静音的噪底被拉爆。
    """
    try:
        with proc_slot(PROC_WAIT):
            proc = subprocess.run(
                [
                    FFMPEG,
                    "-hide_banner",
                    "-nostdin",
                    "-i",
                    file_path,
                    "-map",
                    "0:a:0",
                    "-af",
                    "volumedetect",
                    "-f",
                    "null",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_popen_flags(),
            )
    except FfmpegBusy:
        raise
    except Exception:
        return 0.0
    m = re.search(
        r"max_volume:\s*(-inf|-?[\d.]+)\s*dB", proc.stderr.decode("utf-8", "replace")
    )
    if not m or m.group(1) == "-inf":
        return 0.0
    return max(0.0, min(-float(m.group(1)), 60.0))


def _wave_band(png_file: str, w: int, h: int) -> tuple[int, int]:
    """返回波形内容的竖直裁切范围 (y, height)：对称于中线，带 2px 余量。

    showwavespic 的幅度映射有固有余量（0dBFS ≈ 64% 高），裁切后由
    CSS 拉伸，波形自动填满时间线高度；文件内相对动态保留。
    """
    with proc_slot(PROC_WAIT):
        proc = subprocess.run(
            [
                FFMPEG,
                "-v",
                "error",
                "-i",
                png_file,
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_popen_flags(),
        )
    raw = proc.stdout
    if len(raw) < w * h:
        raise RuntimeError("波形图解码失败")
    top = bottom = None
    for yy in range(h):
        row = raw[yy * w : (yy + 1) * w]
        if any(p > 40 for p in row):
            if top is None:
                top = yy
            bottom = yy
    if top is None:
        return 0, h  # 全黑（纯静音）→ 不裁切
    mid = (h - 1) / 2
    half = max(bottom - mid, mid - top) + 2
    half = max(half, 16.0)  # 纯平线不过度放大
    half = min(half, h / 2)
    y = int(round(mid - half))
    hh = int(round(2 * half))
    hh = min(hh, h - y)
    hh -= hh % 2
    return (y, hh) if hh >= 2 else (0, h)


@_coalesce("wave")
def build_timeline_assets(file_path: str) -> dict:
    """生成时间线波形素材（时间线只显示音频波形；点击跳转由前端处理）。

    波形输入用已就绪的纯音频缓存，避免对原视频重复 demux；时长取自源文件，
    音轨比视频轨短时波形宽度不跟着缩。缓存未就绪时读原文件。
    幅度自动缩放：先按峰值归一（安静录音自动放大），再按内容裁切填满显示。
    返回结构带 reason 字段：ok（命中缓存或生成成功）/
    no_audio（无音轨）/ failed（有音轨但生成失败，附 error 明细）。
    """
    st = os.stat(file_path)
    # wave5：波形按时长等比的动态分辨率 + 自动增益 + 内容裁切，与旧缓存区分
    # key 绑定源文件：音轨是直拷，音频数据不变，已有波形缓存可直接命中
    key = hashlib.sha1(
        f"wave5|{file_path}|{st.st_size}|{st.st_mtime}".encode()
    ).hexdigest()[:16]
    wave_file = CACHE_DIR / f"{key}_wave.png"
    raw_file = CACHE_DIR / f"{key}_wave_raw.png"

    if wave_file.exists():
        return {"wave": f"/api/cache/{wave_file.name}", "reason": "ok"}

    info = probe(file_path)
    if not info.get("audio"):
        return {"wave": None, "reason": "no_audio"}

    # 音轨抽取由载入流程负责；缓存未就绪时读原文件，波形不阻塞在抽取上
    audio_file = audio_cached_file(file_path) or file_path

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        w = _wave_width(info.get("duration") or 0)
        gain = wave_gain_db(audio_file)
        run_collect(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-nostdin",
                "-i",
                audio_file,
                # showwavespic 需整段音频缓冲，先降采样/降位深/转单声道压内存
                "-filter_complex",
                f"volume={gain:.2f}dB,aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono,showwavespic=s={w}x{WAVE_HEIGHT}:colors=#378ADD",
                "-frames:v",
                "1",
                "-update",
                "1",
                str(raw_file),
            ],
            8 * 1024 * 1024,
        )
        y, hh = _wave_band(str(raw_file), w, WAVE_HEIGHT)
        run_collect(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-nostdin",
                "-i",
                str(raw_file),
                "-vf",
                f"crop={w}:{hh}:0:{y}",
                str(wave_file),
            ],
            8 * 1024 * 1024,
        )
        raw_file.unlink(missing_ok=True)
    except Exception as e:
        return {"wave": None, "reason": "failed", "error": str(e)}

    return {
        "wave": f"/api/cache/{wave_file.name}" if wave_file.exists() else None,
        "reason": "ok" if wave_file.exists() else "failed",
    }


def _audio_cache_key(file_path: str) -> str:
    """音频缓存键：绑定源文件大小与修改时间，文件被覆盖后自然失效。"""
    st = os.stat(file_path)
    return hashlib.sha1(
        f"audio1|{file_path}|{st.st_size}|{st.st_mtime}".encode()
    ).hexdigest()[:16]


def audio_cached_file(file_path: str) -> str | None:
    """只查音频缓存，不触发抽取。"""
    cached = sorted(CACHE_DIR.glob(f"{_audio_cache_key(file_path)}_audio.*"))
    return str(cached[0]) if cached else None


# 抽音轨要顺序扫完整个源文件，留在请求线程里会让媒体连接长时间无响应。
# 后台生成后由短请求查状态，避免连接空窗被中间网络重置。
_AUDIO_PREPARE_LOCK = threading.Lock()
_AUDIO_PREPARING: dict[str, dict] = {}


def _new_prepare_slot() -> dict:
    return {
        "error": None,
        "cancel": False,
        "cancelled": False,
        "progress": 0.0,
        # 阶段供前端展示：queued / probe / extract / finalize。
        # -c:a copy 时 ffmpeg 只在整个流拷完才输出一次 progress，
        # 百分比长期停在 0，前端要靠阶段 + 耗时才能看出「在动」。
        "phase": "queued",
        "started": time.monotonic(),
        "done": threading.Event(),
    }


def _prepare_audio_worker(file_path: str, slot: dict) -> None:
    try:
        audio_only_cache(file_path)
    except AudioPrepareCancelled:
        slot["cancelled"] = True
    except BaseException as e:
        if slot.get("cancel"):
            slot["cancelled"] = True
        else:
            slot["error"] = str(e) or type(e).__name__
    finally:
        slot["done"].set()


def prepare_audio_cache(file_path: str, restart: bool = False) -> dict:
    """音频缓存未就绪时交给后台线程生成；本调用只返回当前状态。

    restart 用于终态（失败或已终止）后重新抽取，正在运行的任务不受影响。
    """
    if audio_cached_file(file_path):
        return {"ready": True, "progress": 1.0}
    key = _audio_cache_key(file_path)
    with _AUDIO_PREPARE_LOCK:
        slot = _AUDIO_PREPARING.get(key)
        if restart and slot is not None and slot["done"].is_set():
            _AUDIO_PREPARING.pop(key, None)
            slot = None
        if slot is None:
            slot = _new_prepare_slot()
            _AUDIO_PREPARING[key] = slot
            threading.Thread(
                target=_prepare_audio_worker,
                args=(file_path, slot),
                daemon=True,
            ).start()
        return {
            "ready": False,
            "progress": slot["progress"],
            "phase": slot["phase"],
            "elapsed": round(time.monotonic() - slot["started"], 1),
            "error": slot["error"],
            "cancelled": slot["cancelled"] or slot["cancel"],
        }


def cancel_audio_prepare(file_path: str) -> dict:
    """终止后台抽音轨；返回时子进程已退出，可安全重新发起准备。"""
    key = _audio_cache_key(file_path)
    with _AUDIO_PREPARE_LOCK:
        slot = _AUDIO_PREPARING.get(key)
        if slot is None or slot["done"].is_set():
            return {"cancelled": False}
        slot["cancel"] = True
        proc = slot.get("proc")
        if proc is not None and proc.poll() is None:
            proc.kill()
    slot["done"].wait(timeout=10)
    return {"cancelled": True}


@_coalesce("audio")
def audio_only_cache(file_path: str) -> str:
    """返回仅含音轨的缓存文件，不存在时按源编码直拷生成。

    容器由源音轨编码决定，避免重编码；mp4 系容器把 moov 移到文件头，
    使浏览器在下载完成前即可起播。缓存 key 绑定源文件大小与修改时间。
    """
    cached = audio_cached_file(file_path)
    if cached:
        return cached
    key = _audio_cache_key(file_path)

    # 阶段只用于 /api/prepare 的前端提示；其他调用路径没有 slot 时跳过
    slot = _AUDIO_PREPARING.get(key)
    if slot is not None:
        slot["phase"] = "probe"
    info = probe(file_path)
    audio = info.get("audio")
    if not audio:
        raise ValueError("文件没有音轨")

    ext = _audio_ext(audio.get("codec_name"))
    # m4a 与 mp4 同为 ISO BMFF 容器，统一用 .mp4 让响应按 video/mp4 发出
    if ext == ".m4a":
        ext = ".mp4"
    tmp = _temp_file("audio", ext)
    cmd = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        file_path,
        "-vn",
        "-c:a",
        "copy",
    ]
    if ext in (".mp4", ".m4a", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(tmp)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if slot is None:
            run_collect(cmd, 8 * 1024 * 1024)
        else:
            slot["phase"] = "extract"
            run_progress(cmd, info.get("duration") or 0, slot)
        if slot is not None:
            slot["phase"] = "finalize"
        out = CACHE_DIR / f"{key}_audio{ext}"
        os.replace(tmp, out)
        return str(out)
    finally:
        Path(tmp).unlink(missing_ok=True)


def ffmpeg_version() -> str:
    try:
        out = run_probe([FFMPEG, "-version"], 4 * 1024 * 1024)
        return out.splitlines()[0]
    except Exception as e:
        return f"不可用: {e}"
