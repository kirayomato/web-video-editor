# -*- coding: utf-8 -*-
"""端到端冒烟测试：覆盖 probe/keyframes/timeline/dryRun/各类任务/下载/Range。"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8787"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "tmp", "sample.mp4")

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name} {extra}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {extra}")


def get(path, raw=False):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        return data if raw else json.loads(data.decode("utf-8"))


def post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_task(tid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = get(f"/api/tasks/{tid}")
        if t["status"] in ("done", "error"):
            return t
        time.sleep(0.5)
    return t


def first_packet_pts(file, stream="a:0"):
    """音频首包时间戳（回归：输入侧 -ss + copy 会带负时间戳前摇）。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream,
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", file],
        capture_output=True, text=True,
    ).stdout.split()
    return float(out[0]) if out else None


def main():
    enc = urllib.parse.quote(SAMPLE)

    # 1. keyframes
    kf = get(f"/api/keyframes?path={enc}&at=3.0&window=3")
    frames = kf.get("frames") or []
    check("keyframes 返回", len(frames) > 0, f"共 {len(frames)} 个关键帧")
    if frames:
        print("       关键帧样例:", [round(f["time"], 3) for f in frames[:6]])

    # 2. timeline assets
    tl = get(f"/api/timeline?path={enc}&duration=10")
    check("timeline sprite", bool(tl.get("sprite")), tl.get("sprite"))
    check("timeline wave", bool(tl.get("wave")), tl.get("wave"))
    sprite = get(tl["sprite"], raw=True)
    check("sprite 可访问", len(sprite) > 1000, f"{len(sprite)} bytes")

    # 3. dryRun 命令
    d = post("/api/tasks", {"type": "trim", "path": SAMPLE, "start": 1, "end": 4, "mode": "lossless", "dryRun": True})
    check("trim 无损 dryRun", "-c copy" in d["command"] and "-ss" in d["command"])
    print("       CMD:", d["command"][:120])
    d2 = post("/api/tasks", {"type": "trim", "path": SAMPLE, "start": 1, "end": 4, "mode": "precise", "dryRun": True})
    check("trim 精确 dryRun", "libx264" in d2["command"])

    # 4. trim 无损（真实执行）
    t = post("/api/tasks", {"type": "trim", "path": SAMPLE, "start": 1, "end": 4, "mode": "lossless"})
    r = wait_task(t["id"])
    check("trim 无损任务", r["status"] == "done", f"status={r['status']} err={r.get('error')}")
    if r["status"] == "done":
        out = r["outPath"]
        check("trim 输出存在", os.path.isfile(out), out)
        info = get(f"/api/probe?path={urllib.parse.quote(out)}")
        dur = info.get("duration") or 0
        # 无损语义：输出 = [对齐后的关键帧入点, 出点]；本样例 GOP≈8s，入点 1s 对齐到 0 → 时长 ≈ 4s
        check("trim 无损时长 = 出点-对齐入点", 3.9 <= dur <= 4.3, f"{dur:.2f}s")

    # 5. trim 精确
    t = post("/api/tasks", {"type": "trim", "path": SAMPLE, "start": 2, "end": 5, "mode": "precise", "crf": 20})
    r = wait_task(t["id"])
    check("trim 精确任务", r["status"] == "done", f"err={r.get('error')}")
    if r["status"] == "done":
        info = get(f"/api/probe?path={urllib.parse.quote(r['outPath'])}")
        dur = info.get("duration") or 0
        check("精确输出时长≈3s", 2.7 <= dur <= 3.3, f"{dur:.2f}s")
        pts = first_packet_pts(r["outPath"])
        check("精确截取音频无负时间戳前摇", pts is not None and pts >= -0.05, f"first_pts={pts}")

    # 6. cut-middle
    t = post("/api/tasks", {"type": "cut-middle", "path": SAMPLE, "in": 3, "out": 5, "duration": 10, "mode": "precise"})
    r = wait_task(t["id"])
    check("cut-middle 任务", r["status"] == "done", f"err={r.get('error')}")
    if r["status"] == "done":
        info = get(f"/api/probe?path={urllib.parse.quote(r['outPath'])}")
        dur = info.get("duration") or 0
        check("cut-middle 输出≈8s", 7.5 <= dur <= 8.3, f"{dur:.2f}s")

    # 7. audio extract / remove（音频一律 stream copy）
    t = post("/api/tasks", {"type": "audio", "path": SAMPLE, "action": "extract"})
    r = wait_task(t["id"])
    check("提取音轨 copy", r["status"] == "done", f"err={r.get('error')}")
    if r["status"] == "done":
        info = get(f"/api/probe?path={urllib.parse.quote(r['outPath'])}")
        codec = (info.get("audio") or {}).get("codec_name")
        check("提取音轨编码保持 aac（未转码）", codec == "aac", f"codec={codec}")
    t = post("/api/tasks", {"type": "audio", "path": SAMPLE, "action": "remove"})
    r = wait_task(t["id"])
    check("去除音轨", r["status"] == "done", f"err={r.get('error')}")
    if r["status"] == "done":
        info = get(f"/api/probe?path={urllib.parse.quote(r['outPath'])}")
        check("去音轨后无音轨", info.get("audio") is None)

    # 8. frame
    t = post("/api/tasks", {"type": "frame", "path": SAMPLE, "time": 3.3, "format": "jpg"})
    r = wait_task(t["id"])
    check("截帧 jpg", r["status"] == "done" and os.path.isfile(r.get("outPath") or ""), f"err={r.get('error')}")

    # 9. transcode
    t = post("/api/tasks", {"type": "transcode", "path": SAMPLE, "format": "mp4", "crf": 28, "preset": "ultrafast", "scale": 240})
    r = wait_task(t["id"])
    check("转码任务", r["status"] == "done", f"err={r.get('error')}")
    if r["status"] == "done":
        info = get(f"/api/probe?path={urllib.parse.quote(r['outPath'])}")
        h = (info.get("video") or {}).get("height")
        check("转码高度 240", h == 240, f"height={h}")

    # 10. concat（两段 trim 结果）
    outs = []
    for (s, e) in [(0, 2), (5, 7)]:
        t = post("/api/tasks", {"type": "trim", "path": SAMPLE, "start": s, "end": e, "mode": "precise", "crf": 20})
        r = wait_task(t["id"])
        if r["status"] == "done":
            outs.append(r["outPath"])
            seg = get(f"/api/probe?path={urllib.parse.quote(r['outPath'])}")
            print(f"       段 {s}-{e} 时长: {(seg.get('duration') or 0):.2f}s")
    if len(outs) == 2:
        t = post("/api/tasks", {"type": "concat", "paths": outs, "mode": "lossless"})
        r = wait_task(t["id"])
        check("拼接任务", r["status"] == "done", f"err={r.get('error')}")
        if r["status"] == "done":
            info = get(f"/api/probe?path={urllib.parse.quote(r['outPath'])}")
            dur = info.get("duration") or 0
            check("拼接时长≈4s", 3.6 <= dur <= 4.5, f"{dur:.2f}s")

    # 11. Range 播放
    req = urllib.request.Request(BASE + f"/api/media?path={enc}", headers={"Range": "bytes=0-1023"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        check("media Range 206", resp.status == 206 and len(body) == 1024, f"status={resp.status} len={len(body)}")

    # 12. 下载（Content-Disposition）
    req = urllib.request.Request(BASE + f"/api/download?path={enc}&name=test.mp4")
    with urllib.request.urlopen(req, timeout=30) as resp:
        cd = resp.headers.get("Content-Disposition", "")
        check("download 附件头", "attachment" in cd and "test.mp4" in cd, cd[:80])
        check("download 内容", len(resp.read()) == os.path.getsize(SAMPLE))

    # 13. 静态页面
    page = get("/", raw=True).decode("utf-8", "replace")
    check("首页包含 ArtPlayer 容器", "artplayer" in page and "app.js" in page)
    appjs = get("/app.js", raw=True)
    check("app.js 可访问", len(appjs) > 5000, f"{len(appjs)} bytes")
    vendor = get("/vendor/artplayer.js", raw=True)
    check("vendor artplayer 可访问", len(vendor) > 100000, f"{len(vendor)} bytes")

    # 14. 路径清洗（Windows「复制为路径」带引号；中文+空格文件名）
    sp = os.path.join(ROOT, "tmp", "测试 video.mp4")
    if not os.path.isfile(sp):
        shutil.copyfile(SAMPLE, sp)
    norm = lambda p: os.path.normcase(os.path.normpath(p or ""))
    info = get(f"/api/probe?path={urllib.parse.quote('\"' + sp + '\"')}")
    check("双引号路径可载入", norm(info.get("path")) == norm(sp), info.get("path"))
    info = get(f"/api/probe?path={urllib.parse.quote(chr(39) + sp + chr(39))}")
    check("单引号路径可载入", norm(info.get("path")) == norm(sp))
    err = ""
    try:
        get("/api/probe?path=" + urllib.parse.quote('"Z:\\no_such_dir\\x.mp4"'))
    except urllib.error.HTTPError as e:
        err = json.loads(e.read().decode("utf-8") or "{}").get("error", "")
    check("不存在文件报「文件不存在」", "不存在" in err, err[:80])
    t = post("/api/tasks", {"type": "trim", "path": '"' + SAMPLE + '"', "start": 1, "end": 2, "mode": "lossless", "dryRun": True})
    check("任务提交同样清洗路径", "-c copy" in (t.get("command") or ""), (t.get("command") or "")[:60])

    print(f"\n===== 结果: PASS={PASS} FAIL={FAIL} =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
