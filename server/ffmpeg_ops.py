"""ffmpeg/ffprobe 封装：探测、命令构建、执行、时间线素材生成。零第三方依赖。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
    """执行命令并收集 stdout（用于 ffprobe/短命令）。"""
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_popen_flags(),
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError("\n".join(err.splitlines()[-6:]) or f"{' '.join(args[:2])} 退出码 {proc.returncode}")
    out = proc.stdout[:max_buffer].decode("utf-8", "replace")
    return out


def probe(file_path: str) -> dict:
    raw = run_collect([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", file_path])
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
            base.update({
                "width": s.get("width"),
                "height": s.get("height"),
                "fps": parse_fps(s.get("avg_frame_rate")) or parse_fps(s.get("r_frame_rate")),
                "pix_fmt": s.get("pix_fmt"),
            })
        if s.get("codec_type") == "audio":
            base.update({
                "channels": s.get("channels"),
                "sample_rate": int(s["sample_rate"]) if s.get("sample_rate") else None,
            })
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


def keyframes_near(file_path: str, at: float, window: float = 5.0) -> list:
    start = max(0.0, float(at) - window)
    end = float(at) + window
    # read_intervals 绝对区间语法：START%END（均为绝对秒）；%+N 形式是相对偏移，勿混用
    interval = f"{start:.3f}%{end:.3f}"
    raw = run_collect([
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "frame=pts_time,pict_type",
        "-of", "csv=p=0",
        "-read_intervals", interval,
        file_path,
    ])
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
    "aac": ".m4a", "mp3": ".mp3", "opus": ".opus", "vorbis": ".ogg",
    "flac": ".flac", "ac3": ".ac3", "eac3": ".eac3", "wmav2": ".wma", "pcm_s16le": ".wav",
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
            steps.append(g + [
                "-ss", ff_time(pre), "-i", task["path"], "-ss", ff_time(start - pre), "-t", ff_time(dur),
                "-c:v", "libx264", "-crf", str(task.get("crf", 18)), "-preset", task.get("preset") or "veryfast",
                "-c:a", "copy", "-movflags", "+faststart", out_path,
            ])
        else:
            out_path = _out_path(task["path"], "trim", ".mp4")
            steps.append(g + [
                "-ss", ff_time(start), "-i", task["path"], "-t", ff_time(dur),
                "-c", "copy", "-avoid_negative_ts", "make_zero", out_path,
            ])

    elif t == "cut-middle":
        in_p = float(task.get("in") or 0)
        out_p = float(task.get("out") or 0)
        duration = float(task.get("duration") or 0)
        segs = []
        if in_p > 0.05:
            segs.append((0.0, in_p))
        if duration > 0 and out_p < duration - 0.05:
            segs.append((out_p, duration))
        if not segs:
            raise ValueError("删除区间覆盖了整个视频")
        seg_files = []
        for i, (s, e) in enumerate(segs):
            seg = _temp_file(f"seg{i}", ".mp4")
            if task.get("mode") == "precise":
                # 同 trim 精确：输入侧粗跳 + 输出侧精修，避免音频 copy 负时间戳前摇
                pre = max(0.0, s - 2.0)
                steps.append(g + [
                    "-ss", ff_time(pre), "-i", task["path"], "-ss", ff_time(s - pre), "-t", ff_time(e - s),
                    "-c:v", "libx264", "-crf", str(task.get("crf", 18)), "-preset", task.get("preset") or "veryfast",
                    "-c:a", "copy", seg,
                ])
            else:
                steps.append(g + [
                    "-ss", ff_time(s), "-i", task["path"], "-t", ff_time(e - s),
                    "-c", "copy", "-avoid_negative_ts", "make_zero", seg,
                ])
            seg_files.append(seg)
        out_path = _out_path(task["path"], "cutmiddle", ".mp4")
        lst = _concat_list(seg_files)
        steps.append(g + ["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                          "-avoid_negative_ts", "make_zero", out_path])

    elif t == "concat":
        paths = [str(p).strip() for p in (task.get("paths") or []) if str(p).strip()]
        if len(paths) < 2:
            raise ValueError("拼接至少需要 2 个文件")
        lst = _concat_list(paths)
        if task.get("mode") == "transcode":
            out_path = _out_path(paths[0], "concat_tc", ".mp4")
            # 视频统一转码，音频一律 stream copy
            steps.append(g + [
                "-f", "concat", "-safe", "0", "-i", lst,
                "-c:v", "libx264", "-crf", str(task.get("crf", 20)), "-preset", task.get("preset") or "medium",
                "-c:a", "copy", "-movflags", "+faststart", out_path,
            ])
        else:
            out_path = _out_path(paths[0], "concat", ".mp4")
            steps.append(g + ["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                              "-avoid_negative_ts", "make_zero", out_path])

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
        steps.append(g + ["-ss", ff_time(float(task.get("time") or 0)), "-i", task["path"],
                          "-frames:v", "1", "-q:v", "2", out_path])

    elif t == "transcode":
        fmt = "webm" if task.get("format") == "webm" else "mp4"
        out_path = _out_path(task["path"], "transcode", f".{fmt}")
        args = g + ["-i", task["path"]]
        if task.get("scale"):
            args += ["-vf", f"scale=-2:{int(task['scale'])}"]
        # 视频重编码，音频一律 stream copy
        if fmt == "webm":
            args += ["-c:v", "libvpx-vp9", "-crf", str(task.get("crf", 32)), "-b:v", "0", "-c:a", "copy"]
        else:
            args += ["-c:v", "libx264", "-crf", str(task.get("crf", 23)), "-preset", task.get("preset") or "medium",
                     "-c:a", "copy", "-movflags", "+faststart"]
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
                self.on_progress({
                    "stepIndex": index,
                    "stepCount": total,
                    "progress": progress,
                    "outTime": out_time,
                })

        proc.stdout.close()
        code = proc.wait()
        t_err.join(timeout=2)
        if code != 0:
            if self._cancelled:
                raise RuntimeError("任务已取消")
            err = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
            raise RuntimeError("\n".join(err.splitlines()[-8:]) or f"ffmpeg 退出码 {code}")


def wave_gain_db(file_path: str) -> float:
    """波形显示用自动增益（dB）：把音频峰值拉到 0dBFS，安静录音自动放大。

    只影响波形可视化，不改动任何导出产物。静音/无音轨返回 0；
    增益上限 60dB，防止近乎静音的噪底被拉爆。
    """
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-nostdin", "-i", file_path,
             "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_popen_flags(),
        )
    except Exception:
        return 0.0
    m = re.search(r"max_volume:\s*(-inf|-?[\d.]+)\s*dB", proc.stderr.decode("utf-8", "replace"))
    if not m or m.group(1) == "-inf":
        return 0.0
    return max(0.0, min(-float(m.group(1)), 60.0))


def _wave_band(png_file: str, w: int, h: int) -> tuple[int, int]:
    """返回波形内容的竖直裁切范围 (y, height)：对称于中线，带 2px 余量。

    showwavespic 的幅度映射有固有余量（0dBFS ≈ 64% 高），裁切后由
    CSS 拉伸，波形自动填满时间线高度；文件内相对动态保留。
    """
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-i", png_file, "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_popen_flags(),
    )
    raw = proc.stdout
    if len(raw) < w * h:
        raise RuntimeError("波形图解码失败")
    top = bottom = None
    for yy in range(h):
        row = raw[yy * w:(yy + 1) * w]
        if any(p > 40 for p in row):
            if top is None:
                top = yy
            bottom = yy
    if top is None:
        return 0, h  # 全黑（纯静音）→ 不裁切
    mid = (h - 1) / 2
    half = max(bottom - mid, mid - top) + 2
    half = max(half, 16.0)   # 纯平线不过度放大
    half = min(half, h / 2)
    y = int(round(mid - half))
    hh = int(round(2 * half))
    hh = min(hh, h - y)
    hh -= hh % 2
    return (y, hh) if hh >= 2 else (0, h)


def build_timeline_assets(file_path: str) -> dict:
    """生成时间线波形素材（时间线只显示音频波形；点击跳转由前端处理）。

    幅度自动缩放：先按峰值归一（安静录音自动放大），再按内容裁切填满显示。
    """
    st = os.stat(file_path)
    # wave4：波形自动增益 + 内容裁切，与旧缓存区分
    key = hashlib.sha1(f"wave4|{file_path}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:16]
    wave_file = CACHE_DIR / f"{key}_wave.png"
    raw_file = CACHE_DIR / f"{key}_wave_raw.png"

    if not wave_file.exists():
        try:
            gain = wave_gain_db(file_path)
            run_collect([
                FFMPEG, "-y", "-hide_banner", "-nostdin", "-i", file_path,
                "-filter_complex", f"volume={gain:.2f}dB,showwavespic=s=1600x160:colors=#378ADD",
                "-frames:v", "1", str(raw_file),
            ], 8 * 1024 * 1024)
            y, hh = _wave_band(str(raw_file), 1600, 160)
            run_collect([
                FFMPEG, "-y", "-hide_banner", "-nostdin", "-i", str(raw_file),
                "-vf", f"crop=1600:{hh}:0:{y}", str(wave_file),
            ], 8 * 1024 * 1024)
            raw_file.unlink(missing_ok=True)
        except Exception:
            pass  # 无音轨 → 无波形

    return {
        "wave": f"/api/cache/{wave_file.name}" if wave_file.exists() else None,
    }


def ffmpeg_version() -> str:
    try:
        out = run_collect([FFMPEG, "-version"], 4 * 1024 * 1024)
        return out.splitlines()[0]
    except Exception as e:
        return f"不可用: {e}"
