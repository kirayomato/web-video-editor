"""内存任务队列（串行执行）+ SSE 订阅。"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

from ffmpeg_ops import assert_local_path, build_command, probe, StepRunner


class TaskStore:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.listeners: dict[str, set] = {}
        self.lock = threading.Lock()
        self.queue: list[str] = []
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    # ---------- 公共接口 ----------

    def create(self, body: dict) -> dict:
        task = dict(body)
        self._validate(task)

        duration = None
        if task["type"] != "concat":
            try:
                info = probe(task["path"])
                duration = info.get("duration")
                if task["type"] == "cut-middle" and not task.get("duration"):
                    task["duration"] = duration
                if task["type"] == "audio" and task.get("action") == "extract" and info.get("audio"):
                    task["_audio_codec"] = info["audio"].get("codec_name")
            except Exception as e:
                raise RuntimeError(f"ffprobe 失败: {e}") from e
        elif task["type"] == "cut-middle" and not task.get("duration"):
            info = probe(task["path"])
            task["duration"] = info.get("duration")

        built = build_command(task)
        out_path = built["outPath"]
        if task.get("dryRun"):
            return {
                "dryRun": True,
                "command": built["command"],
                "outPath": out_path,
                "outName": os.path.basename(out_path),
            }

        tid = "t_" + uuid.uuid4().hex[:12]
        record = {
            "id": tid,
            "type": task["type"],
            "label": self._label(task),
            "status": "queued",
            "progress": 0.0,
            "outTime": 0.0,
            "command": built["command"],
            "outPath": out_path,
            "outName": os.path.basename(out_path),
            "error": None,
            "createdAt": time.time(),
            "duration": duration,
            "_built": built,
        }
        with self.lock:
            self.tasks[tid] = record
            self.queue.append(tid)
        self._emit(tid, {"type": "status", **self.snapshot(tid)})
        return self.snapshot(tid)

    def get(self, tid: str):
        return self.snapshot(tid)

    def list(self) -> list:
        with self.lock:
            items = sorted(self.tasks.values(), key=lambda t: t["createdAt"], reverse=True)
            return [self._snap(t) for t in items]

    def cancel(self, tid: str) -> bool:
        with self.lock:
            t = self.tasks.get(tid)
            if not t:
                return False
            if t.get("_runner"):
                t["_runner"].cancel()
                return True
            if t["status"] == "queued":
                t["status"] = "error"
                t["error"] = "任务已取消"
            else:
                return False
        self._emit(tid, {"type": "status", **self.snapshot(tid)})
        return True

    def subscribe(self, tid: str, fn):
        with self.lock:
            self.listeners.setdefault(tid, set()).add(fn)

        def unsubscribe():
            with self.lock:
                s = self.listeners.get(tid)
                if s:
                    s.discard(fn)
                    if not s:
                        self.listeners.pop(tid, None)

        return unsubscribe

    def snapshot(self, tid: str):
        with self.lock:
            t = self.tasks.get(tid)
            return self._snap(t) if t else None

    # ---------- 内部 ----------

    @staticmethod
    def _snap(t: dict) -> dict:
        return {
            "id": t["id"],
            "type": t["type"],
            "status": t["status"],
            "progress": t["progress"],
            "outTime": t["outTime"],
            "label": t["label"],
            "command": t["command"],
            "outPath": t["outPath"] if t["status"] == "done" else None,
            "outName": t["outName"],
            "error": t["error"],
            "createdAt": t["createdAt"],
            "duration": t["duration"],
        }

    @staticmethod
    def _validate(task: dict):
        # 清洗并规范化路径（兼容带引号粘贴），同时要求文件存在
        if task.get("type") in ("trim", "cut-middle", "audio", "frame", "transcode"):
            if not task.get("path"):
                raise ValueError("缺少文件路径 path")
            task["path"] = assert_local_path(task["path"])
        if task.get("type") == "concat":
            paths = task.get("paths")
            if not isinstance(paths, list) or len(paths) < 2:
                raise ValueError("拼接需要 paths 数组（至少 2 个）")
            task["paths"] = [assert_local_path(p) for p in paths]

    @staticmethod
    def _label(task: dict) -> str:
        t = task.get("type")
        if t == "trim":
            mode = "精确" if task.get("mode") == "precise" else "无损"
            return f"截取 {float(task.get('start') or 0):.2f}s ~ {float(task.get('end') or 0):.2f}s（{mode}）"
        if t == "cut-middle":
            return f"删除 {float(task.get('in') or 0):.2f}s ~ {float(task.get('out') or 0):.2f}s"
        if t == "concat":
            return f"拼接 {len(task.get('paths') or [])} 个文件"
        if t == "audio":
            return "提取音轨" if task.get("action") == "extract" else "去除音轨"
        if t == "frame":
            return f"截帧 @ {float(task.get('time') or 0):.2f}s"
        if t == "transcode":
            fmt = "WebM" if task.get("format") == "webm" else "MP4"
            scale = f" · {task['scale']}p" if task.get("scale") else ""
            return f"转码（{fmt}{scale}）"
        return str(t)

    def _emit(self, tid: str, event: dict):
        with self.lock:
            fns = list(self.listeners.get(tid, ()))
        for fn in fns:
            try:
                fn(event)
            except Exception:
                pass

    def _loop(self):
        while True:
            tid = None
            with self.lock:
                if self.queue:
                    tid = self.queue.pop(0)
            if tid is None:
                time.sleep(0.1)
                continue
            self._execute(tid)

    def _execute(self, tid: str):
        with self.lock:
            t = self.tasks.get(tid)
            if not t:
                return
            built = t.pop("_built", None)
            if not built:
                return
            t["status"] = "running"
            t["progress"] = 0.0
        self._emit(tid, {"type": "status", **self.snapshot(tid)})

        def on_progress(p):
            total = p.get("stepCount") or 1
            idx = p.get("stepIndex") or 0
            prog = p.get("progress")
            overall = (idx + (prog if prog is not None else 0.5)) / total
            with self.lock:
                t["progress"] = min(1.0, overall)
                t["outTime"] = p.get("outTime") or 0.0
            self._emit(tid, {"type": "progress", **self.snapshot(tid)})

        runner = StepRunner(built["steps"], duration=t.get("duration"), on_progress=on_progress)
        with self.lock:
            t["_runner"] = runner
        try:
            runner.run()
            with self.lock:
                t["status"] = "done"
                t["progress"] = 1.0
            self._emit(tid, {"type": "status", **self.snapshot(tid)})
        except Exception as e:
            with self.lock:
                t["status"] = "error"
                t["error"] = str(e)
            self._emit(tid, {"type": "status", **self.snapshot(tid)})
        finally:
            with self.lock:
                t["_runner"] = None


store = TaskStore()
