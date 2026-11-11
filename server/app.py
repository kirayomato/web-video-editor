"""网页视频剪辑器 — 本地服务（Python 标准库，零第三方依赖）。

启动: python server/app.py   （默认 http://127.0.0.1:7788）
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ffmpeg_ops import (  # noqa: E402
    CACHE_DIR,
    FFMPEG,
    FFPROBE,
    OUT_DIR,
    UPLOAD_DIR,
    assert_local_dir,
    assert_local_path,
    build_timeline_assets,
    clean_path_str,
    ffmpeg_version,
    keyframes_near,
    probe,
)
from tasks import store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
PORT = int(os.environ.get("PORT", "7788"))
HOST = os.environ.get("HOST", "0.0.0.0")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".txt": "text/plain; charset=utf-8",
}


def file_stat(p: str):
    try:
        st = os.stat(p)
        return st if os.path.isfile(p) else None
    except OSError:
        return None


# 本地文件浏览器：常见视频/音频扩展名（过滤用）
MEDIA_EXTS = {
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".avi",
    ".flv",
    ".m4v",
    ".ts",
    ".mts",
    ".m2ts",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".mpe",
    ".3gp",
    ".ogv",
    ".rm",
    ".rmvb",
    ".vob",
    ".divx",
    ".m4a",
    ".mp3",
    ".wav",
    ".aac",
    ".flac",
    ".opus",
    ".ogg",
    ".wma",
    ".mka",
    ".ape",
    ".aiff",
}


def list_roots() -> list:
    """列出可浏览的根（Windows 盘符 / Unix 根）。"""
    if os.name == "nt":
        roots = [
            f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")
        ]
        return roots or ["C:\\"]
    return ["/"]


def list_dir(path: str, show_all: bool = False) -> dict:
    """列出目录内容：文件夹恒显示，文件按媒体扩展名过滤（show_all=True 时全显示）。"""
    p = assert_local_dir(path)
    entries = []
    try:
        names = os.listdir(p)
    except PermissionError as e:
        raise ValueError(f"无权限访问目录：{p}") from e
    for name in names:
        full = os.path.join(p, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        is_dir = os.path.isdir(full)
        ext = "" if is_dir else os.path.splitext(name)[1].lower()
        if not is_dir and not show_all and ext not in MEDIA_EXTS:
            continue
        entries.append(
            {
                "name": name,
                "path": full,
                "isDir": is_dir,
                "size": 0 if is_dir else st.st_size,
                "mtime": st.st_mtime,
                "ext": ext,
                "media": (not is_dir) and ext in MEDIA_EXTS,
            }
        )
    entries.sort(key=lambda e: (not e["isDir"], e["name"].lower()))
    norm = os.path.normpath(p)
    parent = os.path.dirname(norm)
    if parent == norm:  # 盘符根 / Unix 根
        parent = None
    return {"path": norm, "parent": parent, "entries": entries, "total": len(entries)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "web-video-editor/0.1"

    # ---------- 基础响应 ----------

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str):
        self._json(code, {"error": message})

    def _read_body(self, limit: int = 1024 * 1024) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("请求体过大")
        return self.rfile.read(length) if length else b""

    # ---------- 静态 / 文件 ----------

    def _serve_file(self, file_path: str, download=False, filename=None):
        st = file_stat(file_path)
        if not st:
            return self._error(404, "文件不存在")
        size = st.st_size
        ext = os.path.splitext(file_path)[1].lower()
        mime = MIME.get(ext, "application/octet-stream")

        headers = {
            "Content-Type": mime,
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        }
        if download:
            name = filename or os.path.basename(file_path)
            from urllib.parse import quote

            headers["Content-Disposition"] = (
                f"attachment; filename*=UTF-8''{quote(name)}"
            )

        range_header = self.headers.get("Range")
        start = end = None
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if m:
                if m.group(1) == "" and m.group(2) != "":
                    start = max(0, size - int(m.group(2)))
                    end = size - 1
                else:
                    start = int(m.group(1)) if m.group(1) else 0
                    end = int(m.group(2)) if m.group(2) else size - 1
                    if end >= size:
                        end = size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

        if start is not None:
            length = end - start + 1
            self.send_response(206)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def _serve_static(self, rel: str):
        safe = os.path.normpath(rel).lstrip("./\\")
        file_path = os.path.abspath(os.path.join(PUBLIC_DIR, safe))
        if not file_path.startswith(str(PUBLIC_DIR)):
            return self._error(403, "禁止访问")
        if not file_stat(file_path):
            return self._error(404, "资源不存在")
        ext = os.path.splitext(file_path)[1].lower()
        self._serve_file(file_path)
        # 注意：_serve_file 已发送 Cache-Control: no-store；静态资源用短缓存
        # （为简化实现，统一 no-store，对本地工具无性能影响）

    # ---------- 路由 ----------

    def do_GET(self):
        self._route(body=False)

    def do_HEAD(self):
        self._route(body=False)

    def do_POST(self):
        self._route(body=True)

    def _route(self, body=False):
        try:
            parsed = urlparse(self.path)
            p = unquote(parsed.path)
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}

            if p.startswith("/api/"):
                return self._api(p, q, body)
            if p in ("/", "/index.html"):
                return self._serve_static("index.html")
            return self._serve_static(p.lstrip("/"))
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except Exception as e:
            try:
                self._error(500, str(e))
            except Exception:
                pass

    def _api(self, p: str, q: dict, body: bool):
        method = "POST" if body else "GET"

        if p == "/api/health" and method == "GET":
            return self._json(
                200,
                {
                    "ok": True,
                    "ffmpeg": FFMPEG,
                    "ffprobe": FFPROBE,
                    "version": ffmpeg_version(),
                },
            )

        if p == "/api/probe" and method == "GET":
            try:
                return self._json(200, probe(assert_local_path(q.get("path"))))
            except Exception as e:
                return self._error(400, str(e))

        if p == "/api/keyframes" and method == "GET":
            try:
                file_path = assert_local_path(q.get("path"))
                at = float(q.get("at") or 0)
                win = float(q.get("window") or 5)
                return self._json(200, {"frames": keyframes_near(file_path, at, win)})
            except Exception as e:
                return self._error(400, str(e))

        if p == "/api/timeline" and method == "GET":
            try:
                file_path = assert_local_path(q.get("path"))
                return self._json(200, build_timeline_assets(file_path))
            except Exception as e:
                return self._error(400, str(e))

        if p == "/api/fs/roots" and method == "GET":
            return self._json(
                200, {"roots": list_roots(), "home": os.path.expanduser("~")}
            )

        if p == "/api/fs/list" and method == "GET":
            try:
                show_all = (q.get("all") or "").lower() in ("1", "true", "yes")
                return self._json(200, list_dir(q.get("path"), show_all=show_all))
            except Exception as e:
                return self._error(400, str(e))

        if p == "/api/media" and method in ("GET", "HEAD"):
            try:
                return self._serve_file(assert_local_path(q.get("path")))
            except Exception as e:
                return self._error(400, str(e))

        if p == "/api/download" and method in ("GET", "HEAD"):
            try:
                file_path = assert_local_path(q.get("path"))
                name = q.get("name") or os.path.basename(file_path)
                return self._serve_file(file_path, download=True, filename=name)
            except Exception as e:
                return self._error(400, str(e))

        if p == "/api/upload" and method == "POST":
            try:
                raw_name = q.get("name") or "upload.mp4"
                name = (
                    re.sub(r"[^\w.\u4e00-\u9fa5-]+", "_", os.path.basename(raw_name))
                    or "upload.mp4"
                )
                dest = UPLOAD_DIR / f"{int(__import__('time').time() * 1000)}_{name}"
                length = int(self.headers.get("Content-Length") or 0)
                with open(dest, "wb") as f:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                return self._json(200, {"path": str(dest), "name": dest.name})
            except Exception as e:
                return self._error(500, str(e))

        if p == "/api/cleanup" and method == "POST":
            try:
                raw = self._read_body()
                payload = json.loads(raw.decode("utf-8") or "{}")
                days = payload.get("days")
                days = float(7 if days is None else days)
                if days < 1 or days > 365:
                    return self._error(
                        400, "days 需为 1~365 之间的整数（0 表示不清理）"
                    )
                cutoff = time.time() - days * 86400
                deleted = 0
                freed = 0
                files = []
                try:
                    names = os.listdir(OUT_DIR)
                except OSError:
                    names = []
                for name in names:
                    fp = os.path.join(OUT_DIR, name)
                    if not os.path.isfile(fp):
                        continue
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    if st.st_mtime < cutoff:
                        try:
                            freed += st.st_size
                            os.remove(fp)
                            deleted += 1
                            files.append(name)
                        except OSError:
                            pass
                return self._json(
                    200,
                    {
                        "ok": True,
                        "dir": str(OUT_DIR),
                        "days": days,
                        "deleted": deleted,
                        "bytes": freed,
                        "files": files,
                    },
                )
            except Exception as e:
                return self._error(400, str(e))

        if p.startswith("/api/cache/") and method in ("GET", "HEAD"):
            name = os.path.basename(p[len("/api/cache/") :])
            file_path = os.path.abspath(os.path.join(CACHE_DIR, name))
            if not file_path.startswith(str(CACHE_DIR)):
                return self._error(403, "禁止访问")
            return self._serve_file(file_path)

        if p == "/api/tasks" and method == "GET":
            return self._json(200, {"tasks": store.list()})

        if p == "/api/tasks" and method == "POST":
            try:
                raw = self._read_body()
                payload = json.loads(raw.decode("utf-8") or "{}")
                result = store.create(payload)
                return self._json(202 if not payload.get("dryRun") else 200, result)
            except Exception as e:
                return self._error(400, str(e))

        m = re.match(r"^/api/tasks/([^/]+)(/events|/cancel)?$", p)
        if m and method == "GET":
            tid, sub = m.group(1), m.group(2)
            t = store.get(tid)
            if not t:
                return self._error(404, "任务不存在")
            if sub == "/events":
                return self._sse(tid, t)
            return self._json(200, t)

        if m and method == "POST" and m.group(2) == "/cancel":
            ok = store.cancel(m.group(1))
            return self._json(
                200 if ok else 400, {"ok": True} if ok else {"error": "无法取消"}
            )

        return self._error(404, "未知接口")

    def _sse(self, tid: str, snapshot: dict):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send(event: dict):
            try:
                data = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        send({"type": "status", **snapshot})
        done = threading.Event()

        def on_event(event: dict):
            send(event)
            if event.get("type") == "status" and event.get("status") in (
                "done",
                "error",
            ):
                done.set()

        unsubscribe = store.subscribe(tid, on_event)
        try:
            # 若任务已在终态，补发后立即结束
            current = store.get(tid)
            if current and current.get("status") in ("done", "error"):
                done.set()
            done.wait(timeout=3600)
        finally:
            unsubscribe()


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[web-video-editor] http://{HOST}:{PORT}")
    print(f"[web-video-editor] ffmpeg  = {FFMPEG}")
    print(f"[web-video-editor] ffprobe = {FFPROBE}")
    print(f"[web-video-editor] 输出目录 = {OUT_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web-video-editor] 已停止")


if __name__ == "__main__":
    main()
