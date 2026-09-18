#!/usr/bin/env python3
"""
日语跟读（シャドーイング）练习工具的本地网页版。

    python app.py            # 然后浏览器打开 http://localhost:8765

流程：选一篇新闻 → 截一句做参考 → 在浏览器里录自己的跟读 → 自动给出形状/幅度/速度三个分数和对比图。

服务器只用 Python 标准库的 http.server，没有 web 框架，也没有前端构建步骤。
只监听 127.0.0.1，不对外网开放；音频、录音、练习记录全部留在本机，不会上传到任何地方。

依赖：praat-parselmouth、matplotlib、numpy，以及系统里的 ffmpeg。
数据目录（运行时自动创建）：
    news/         抓下来的新闻文本和音频
    segments/     从新闻里截出来的参考句
    recordings/   你自己的录音
    results/      对比图
    history.jsonl 每次练习的分数
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import threading
import webbrowser

# 打包成 .app/.exe 后：代码在临时目录（sys._MEIPASS），数据放在可执行文件旁边的 ShadowingData/
FROZEN = getattr(sys, "frozen", False)
CODE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
if FROZEN:
    exe_dir = os.path.dirname(sys.executable)
    if sys.platform == "darwin" and ".app/Contents/MacOS" in exe_dir:  # 放到 .app 旁边，而不是包内部
        exe_dir = os.path.dirname(exe_dir.split(".app/Contents/MacOS")[0])
    ROOT = os.path.join(exe_dir, "ShadowingData")
    os.makedirs(ROOT, exist_ok=True)
else:
    ROOT = CODE
os.chdir(ROOT)
sys.path.insert(0, CODE)
import compare_pitch  # noqa: E402
import align_mora  # noqa: E402

PORT = 8765
for d in ("news", "segments", "recordings", "results"):
    os.makedirs(d, exist_ok=True)

MIME = {".html": "text/html; charset=utf-8", ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".m4a": "audio/mp4", ".webm": "audio/webm", ".png": "image/png", ".txt": "text/plain; charset=utf-8"}


def safe(rel: str) -> str:
    """把相对路径限制在本目录内。"""
    p = os.path.abspath(os.path.join(ROOT, rel))
    if not p.startswith(ROOT + os.sep):
        raise PermissionError(rel)
    return p


def list_news():
    out = []
    for date in sorted(os.listdir("news"), reverse=True):
        d = os.path.join("news", date)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".txt"):
                base = f[:-4]
                mp3 = os.path.join(d, base + ".mp3")
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    title = fh.readline().strip()
                out.append({"date": date, "title": re.sub(r"（[^（）]*）", "", title), "title_ruby": title,
                            "txt": os.path.join(d, f), "mp3": mp3 if os.path.exists(mp3) else None})
    return out


def article_alignment(mp3: str):
    """整篇文章的按拍对齐结果（缓存在 xxx.align.json），没有就返回 None。"""
    cache = mp3[:-4] + ".align.json"
    if os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))
    return None


ALIGN_STATUS = {"state": "idle", "msg": "", "started": 0, "mp3": None, "result": None}


def _model_cached() -> bool:
    return os.path.exists(os.path.expanduser("~/.cache/torch/hub/checkpoints/model.pt"))


def build_article_alignment(txt: str, mp3: str):
    """在后台线程里跑；进度写进 ALIGN_STATUS，终端同步打印。"""
    def stage(msg):
        ALIGN_STATUS["msg"] = msg
        print(f"[对齐] {msg}  ({time.time() - ALIGN_STATUS['started']:.0f}s)", flush=True)
    try:
        ALIGN_STATUS.update(state="running", started=time.time(), mp3=mp3, result=None)
        with open(txt, encoding="utf-8") as f:
            body = "".join(f.read().split("\n")[2:])  # 去掉标题、链接
        if align_mora._model is None:
            stage("加载模型（第一次要下载约 1.2GB，看终端里的进度条）" if not _model_cached() else "加载模型")
            align_mora._load_model()
        stage("对齐整篇文章（55 秒的新闻在 CPU 上大约 20–60 秒）")
        res = align_mora.align(mp3, body)
        with open(mp3[:-4] + ".align.json", "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
        low = sum(1 for r in res if r["score"] < 0.3)
        ALIGN_STATUS.update(state="done", result={"mora": len(res), "low": low})
        stage(f"完成：{len(res)} 拍，{low} 拍置信度低")
    except Exception as e:
        traceback.print_exc()
        ALIGN_STATUS.update(state="error", msg=str(e)[:300])


HISTORY = "history.jsonl"


def _jsonable(o):
    """numpy 的数字/布尔转成 Python 原生类型。"""
    import numpy as np
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


def history_append(entry: dict):
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=_jsonable) + "\n")


def history_read(ref: str | None = None) -> list:
    if not os.path.exists(HISTORY):
        return []
    out = []
    with open(HISTORY, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ref is None or e.get("ref") == ref:
                out.append(e)
    return out


def history_summary() -> dict:
    h = history_read()
    week = time.time() - 7 * 86400
    return {"attempts": len(h), "sentences": len({e.get("ref") for e in h}),
            "week": sum(1 for e in h if e.get("ts", 0) >= week),
            "ok": sum(1 for e in h if e.get("ok"))}


def list_segments():
    out = []
    for f in sorted(os.listdir("segments"), key=lambda x: os.path.getmtime(os.path.join("segments", x)), reverse=True):
        if f.endswith((".mp3", ".wav")):
            meta = os.path.join("segments", f + ".json")
            info = json.load(open(meta, encoding="utf-8")) if os.path.exists(meta) else {}
            out.append({"path": os.path.join("segments", f), "name": f, **info})
    return out


def split_by_silence(src: str, min_silence=0.4, noise_db=-40):
    """用 ffmpeg silencedetect 按停顿把整篇音频切成句子候选，返回 [(start, end)]。
    阈值放宽到 -40dB，句尾多留 0.3s（不超过静音区），避免吃掉衰减的最后一个音。"""
    r = subprocess.run([compare_pitch.FFMPEG, "-i", src, "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    silences = sorted(zip(starts, ends + [dur] * (len(starts) - len(ends))))
    chunks, pos = [], 0.0           # pos = 当前句子的起点（上一段静音的结束）
    prev_sil_start = 0.0
    for a, b in silences:
        if a - pos >= 0.4:
            start = max(prev_sil_start, pos - 0.15)
            end = min(a + 0.3, b)      # 尾巴多留 0.3s，但不越过静音区
            chunks.append((round(start, 2), round(end, 2)))
        prev_sil_start = a
        pos = b
    if dur - pos >= 0.4:
        chunks.append((round(max(prev_sil_start, pos - 0.15), 2), round(dur, 2)))
    return chunks


def ffmpeg(*args):
    r = subprocess.run([compare_pitch.FFMPEG, "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.strip()[-400:])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静一点
        pass

    # ---------- 工具 ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=_jsonable).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        """支持 Range（Safari 播放/拖动进度条必须）。"""
        size = os.path.getsize(path)
        ctype = MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if b and a else end
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(end - start + 1))

    def body_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    # ---------- GET ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                return self.send_file(os.path.join(CODE, "web", "index.html"))
            if u.path == "/api/news":
                return self.send_json({"news": list_news(), "segments": list_segments(),
                                       "tts": shutil.which("say") is not None, "frozen": FROZEN, "root": ROOT,
                                       "align": align_mora.available(), "summary": history_summary()})
            if u.path == "/api/history":
                return self.send_json({"history": history_read(q["ref"][0])})
            if u.path == "/api/article":
                with open(safe(q["path"][0]), encoding="utf-8") as f:
                    return self.send_json({"text": f.read()})
            if u.path == "/api/align_status":
                st = dict(ALIGN_STATUS)
                st["elapsed"] = round(time.time() - st["started"]) if st["state"] == "running" else 0
                return self.send_json(st)
            if u.path == "/api/split":
                mp3 = safe(q["path"][0])
                chunks = split_by_silence(mp3)
                al = article_alignment(mp3)
                texts = ["".join(m["mora"] for m in align_mora.mora_in_range(al, a, b)) for a, b in chunks] if al else None
                return self.send_json({"chunks": chunks, "texts": texts, "aligned": al is not None})
            if u.path == "/media":
                return self.send_file(safe(q["path"][0]))
            self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    # ---------- POST ----------
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/fetch":
                if FROZEN:  # 打包版没有独立的 python，直接在进程内调用
                    import contextlib
                    import io
                    import fetch_nhk
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                        try:
                            sys.argv = ["fetch_nhk.py"]
                            fetch_nhk.main()
                        except SystemExit as e:
                            print(e)
                    return self.send_json({"output": buf.getvalue().strip()})
                r = subprocess.run([sys.executable, os.path.join(CODE, "fetch_nhk.py")],
                                   capture_output=True, text=True, timeout=300)
                return self.send_json({"output": (r.stdout + r.stderr).strip()})

            if u.path == "/api/align_article":
                d = self.body_json()
                if not align_mora.available():
                    raise RuntimeError("没装 torch/torchaudio，按拍功能不可用")
                if ALIGN_STATUS["state"] == "running":
                    return self.send_json({"ok": True, "already": True})
                threading.Thread(target=build_article_alignment, args=(safe(d["txt"]), safe(d["mp3"])), daemon=True).start()
                return self.send_json({"ok": True})

            if u.path == "/api/segment":
                d = self.body_json()
                src = safe(d["src"])
                a, b = float(d["start"]), float(d["end"])
                if b - a < 0.2:
                    raise ValueError("截取范围太短（至少 0.2 秒）")
                name = f"{time.strftime('%m%d_%H%M%S')}_{a:.1f}-{b:.1f}s.mp3"
                out = os.path.join("segments", name)
                ffmpeg("-i", src, "-ss", f"{a:.2f}", "-to", f"{b:.2f}", out)
                info = {"label": d.get("label", ""), "src": d["src"], "start": a, "end": b}
                json.dump(info, open(out + ".json", "w", encoding="utf-8"), ensure_ascii=False)
                return self.send_json({"path": out, **info})

            if u.path == "/api/tts":
                d = self.body_json()
                text = d["text"].strip()
                if not text:
                    raise ValueError("文本为空")
                if not shutil.which("say"):
                    raise RuntimeError("系统没有 say 命令（只有 macOS 有）")
                name = f"{time.strftime('%m%d_%H%M%S')}_tts.wav"
                aiff = os.path.join("segments", name + ".aiff")
                out = os.path.join("segments", name)
                subprocess.run(["say", "-v", d.get("voice", "Kyoko"), "-r", str(d.get("rate", 170)), "-o", aiff, text],
                               check=True, capture_output=True)
                ffmpeg("-i", aiff, out)
                os.remove(aiff)
                info = {"label": text[:40], "tts": True}
                json.dump(info, open(out + ".json", "w", encoding="utf-8"), ensure_ascii=False)
                return self.send_json({"path": out, **info})

            if u.path == "/api/compare":
                ref = safe(q["ref"][0])
                ct = self.headers.get("Content-Type", "")
                ext = ".webm" if "webm" in ct else ".wav" if "wav" in ct else ".m4a" if "mp4" in ct else ".bin"
                n = int(self.headers.get("Content-Length", 0))
                stamp = time.strftime("%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
                raw = os.path.join("recordings", f"{stamp}_raw{ext}")
                with open(raw, "wb") as f:
                    f.write(self.rfile.read(n))
                wav = os.path.join("recordings", f"{stamp}.wav")
                ffmpeg("-i", raw, "-ac", "1", "-ar", "16000", wav)
                os.remove(raw)
                png = os.path.join("results", os.path.basename(wav)[:-4] + ".png")
                ref_mora = None
                meta = ref + ".json"
                if os.path.exists(meta):
                    info = json.load(open(meta, encoding="utf-8"))
                    if info.get("src") and info.get("end") is not None:
                        al = article_alignment(safe(info["src"]))
                        if al:
                            ref_mora = align_mora.mora_in_range(al, info["start"], info["end"])
                r = compare_pitch.compare(ref, wav, png, ref_mora=ref_mora)
                r["recording"] = wav
                r["ref"] = q["ref"][0]
                r["ts"] = time.time()
                r["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                history_append({k: v for k, v in r.items() if k not in ("ref_curve", "my_curve", "t")})
                return self.send_json(r)

            if u.path == "/api/quit":
                self.send_json({"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            if u.path == "/api/delete":
                d = self.body_json()
                p = safe(d["path"])
                if not p.startswith(os.path.join(ROOT, "segments")):
                    raise PermissionError("只能删 segments 里的文件")
                for x in (p, p + ".json"):
                    if os.path.exists(x):
                        os.remove(x)
                return self.send_json({"ok": True})
            self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)


if __name__ == "__main__":
    if not (os.path.exists(compare_pitch.FFMPEG) or shutil.which(compare_pitch.FFMPEG)):
        msg = "没找到 ffmpeg。" + ("打包时 bin/ 目录里缺少 ffmpeg。" if FROZEN else "先安装: brew install ffmpeg")
        if FROZEN and sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f'display alert "跟读练习" message "{msg}"'])
        sys.exit(msg)
    srv = None
    port = PORT
    for port in range(PORT, PORT + 10):  # 端口被占就往后找
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit("端口 8765–8774 都被占用了")
    url = f"http://localhost:{port}"
    print(f"打开浏览器访问  {url}   （Ctrl+C 停止）\n数据目录: {ROOT}")
    if FROZEN or "--open" in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
