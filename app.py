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
for d in ("news", "segments", "recordings", "results", "refs"):
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


def target_levels(ref: str) -> dict:
    """参考句的目标调型（高/低两档），给「目标台阶」用。

    取整篇对齐结果里落在这一句的拍，按播音员实测音高逐拍二值化：音高在整句中位数
    以上记为高拍。音高已相对各自中位数归一化，所以 0 就是分界线。
    需要这篇文章先做过按假名对齐；没有就返回空，前端隐藏台阶。
    """
    empty = {"kana": [], "levels": []}
    meta = ref + ".json"
    if not os.path.exists(meta):
        return empty
    info = json.load(open(meta, encoding="utf-8"))
    if not info.get("src") or info.get("end") is None:
        return empty
    al = article_alignment(safe(info["src"]))
    if not al:
        return empty
    mora = align_mora.mora_in_range(al, info["start"], info["end"])
    if not mora:
        return empty
    import numpy as np
    (rt, rp, _rdb, (ra, _rb)), _gap = compare_pitch.reference_contour(ref)
    kana, levels = [], []
    for m in mora:
        a, b = m["start"] - ra, m["end"] - ra      # 对齐结果相对句首，曲线相对检测到的语音起点
        sel = (rt >= a) & (rt <= b) & ~np.isnan(rp)
        kana.append(m["mora"])
        levels.append(int(np.mean(rp[sel]) >= 0) if sel.any() else None)
    return {"kana": kana, "levels": levels}


def analyze_pron(wav: str, kana: str) -> dict:
    """把一段录音对到给定假名上，返回每拍的确信度和高低。模式① 和模式② 共用。

    确信度直接用对齐模型的 posterior（align_mora 的 score），不另造分数。
    高低＝该拍平均音高相对整段中位数在上还是在下，和模式③ 的目标台阶同一套口径。
    """
    import numpy as np
    res = align_mora.align(wav, kana)              # [{mora,start,end,score}]
    t, f0, _db = compare_pitch.analyze(compare_pitch.load(wav))
    med = np.nanmedian(f0)
    semis = 12 * np.log2(f0 / med) if med and not np.isnan(med) else np.full_like(f0, np.nan)
    out = []
    for m in res:
        sel = (t >= m["start"]) & (t <= m["end"]) & ~np.isnan(semis)
        v = float(np.mean(semis[sel])) if sel.any() else None
        out.append({"mora": m["mora"], "score": m["score"],
                    "pitch": None if v is None else round(v, 2),
                    "level": None if v is None else int(v >= 0)})
    scores = [m["score"] for m in res] or [0.0]
    return {"mora": out,
            "levels": [m["level"] for m in out],
            "confidence": round(float(sum(scores) / len(scores)), 3)}


def save_upload(ctype: str, raw: bytes) -> str:
    """把上传的录音落盘并转成 16k 单声道 wav，返回 wav 路径。"""
    ext = ".webm" if "webm" in ctype else ".wav" if "wav" in ctype else ".m4a" if "mp4" in ctype else ".bin"
    stamp = time.strftime("%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    src = os.path.join("recordings", f"{stamp}_raw{ext}")
    with open(src, "wb") as f:
        f.write(raw)
    wav = os.path.join("recordings", f"{stamp}.wav")
    ffmpeg("-i", src, "-ac", "1", "-ar", "16000", wav)
    os.remove(src)
    return wav


SAY_LOCK = threading.Lock()
MIN_WAV = 1000          # 16k 单声道 16bit：小于这个长度说明 say 没真的出声（空文件只有 78 字节的头）


def say_ref(text: str, voice: str = "Kyoko", rate: int = 170, tempo: float = 1.0, pad: float = 0.0) -> str:
    """模式① / ② 的参考音：系统朗读，按内容缓存，不往 segments/ 里堆文件。

    单个假名合成出来只有 0.3 秒左右（rate 调再慢也几乎不变），播放时开头还容易被吃掉，
    所以播放用的版本会做时间拉伸（atempo，变速不变调）并前后补静音。
    基线（baseline_conf）仍用 tempo=1 / pad=0 的原始音，免得改变模型看到的东西。

    整个合成过程加锁并写临时文件再原子改名：/api/say 和 /api/baseline 会对同一个音
    算出同一个路径，并发时两个 say 写同一个 .aiff，先转完的那个删掉它，另一个就炸了；
    更糟的是会把零采样的空 wav 留在缓存里，之后每次命中缓存都失败且不会自愈。
    """
    import hashlib
    os.makedirs("refs", exist_ok=True)
    tempo = min(2.0, max(0.5, float(tempo)))        # atempo 的有效下限就是 0.5
    pad = min(1.0, max(0.0, float(pad)))
    key = hashlib.sha1(f"{text}|{voice}|{rate}|{tempo}|{pad}".encode("utf-8")).hexdigest()[:16]
    out = os.path.join("refs", key + ".wav")
    if os.path.exists(out):
        if os.path.getsize(out) >= MIN_WAV:
            return out
        os.remove(out)                      # 之前版本留下的空文件，删掉重来
    if not shutil.which("say"):
        raise RuntimeError("系统没有 say 命令（只有 macOS 有）")
    with SAY_LOCK:
        if os.path.exists(out) and os.path.getsize(out) >= MIN_WAV:
            return out                      # 等锁期间别人已经做好了
        tmp = f"{out}.{os.getpid()}.{threading.get_ident()}"
        aiff, wav = tmp + ".aiff", tmp + ".wav"
        try:
            subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", aiff, text],
                           check=True, capture_output=True)
            filters = []
            if tempo != 1.0:
                filters.append(f"atempo={tempo}")
            if pad:
                filters.append(f"adelay={int(pad * 1000)}")
                filters.append(f"apad=pad_dur={pad}")
            if filters:
                ffmpeg("-i", aiff, "-ac", "1", "-ar", "16000", "-filter:a", ",".join(filters), wav)
            else:
                ffmpeg("-i", aiff, wav)
            if os.path.getsize(wav) < MIN_WAV:
                raise RuntimeError(f"系统朗读没有发出声音：{text!r}")
            os.replace(wav, out)            # 原子改名，缓存里不会出现半成品
        finally:
            for f in (aiff, wav):
                if os.path.exists(f):
                    os.remove(f)
    return out



BASELINE_FILE = "baseline.json"
BASELINE_LOCK = threading.Lock()


def baseline_conf(kana: str) -> float:
    """这个音「读得完全正确」时的确信度是多少。

    用系统朗读合成同一个音，量它自己的对齐确信度。单拍 posterior 本来就低
    （实测 104 个音中位只有 0.47），所以固定的 60% 目标线没有意义；
    只有拿用户的分数跟同一个音的参考音比，才说得出「像不像」。结果按音缓存。
    """
    def load():
        if not os.path.exists(BASELINE_FILE):
            return {}
        try:
            return json.load(open(BASELINE_FILE, encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    cached = load().get(kana)
    if cached is not None:
        return cached
    import numpy as np
    res = align_mora.align(say_ref(kana), kana)
    v = round(float(np.mean([m["score"] for m in res])), 3)
    with BASELINE_LOCK:                     # 读-改-写，并发时不能丢别人刚写的音
        cache = load()
        cache[kana] = v
        with open(BASELINE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    return v


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




# 五十音図：行布局写死，罗马字从 align_mora._ROMA 取，保证表里每个音
# 都是对齐模型认识的（否则它会退回成 a，确信度就没意义了）。
GOJUON_ROWS = {
    "清音": ["あいうえお", "かきくけこ", "さしすせそ", "たちつてと", "なにぬねの",
             "はひふへほ", "まみむめも", "や ゆ よ", "らりるれろ", "わ を ", "ん"],
    "濁音・半濁音": ["がぎぐげご", "ざじずぜぞ", "だぢづでど", "ばびぶべぼ", "ぱぴぷぺぽ"],
    "拗音": ["きゃきゅきょ", "しゃしゅしょ", "ちゃちゅちょ", "にゃにゅにょ", "ひゃひゅひょ",
             "みゃみゅみょ", "りゃりゅりょ", "ぎゃぎゅぎょ", "じゃじゅじょ", "びゃびゅびょ", "ぴゃぴゅぴょ"],
}


def gojuon():
    """[{section, rows:[[{kana, roman} | None, ...]]}]，None 是表里的空位（や行的 い/え 等）。"""
    out = []
    for sec, rows in GOJUON_ROWS.items():
        grid = []
        for r in rows:
            cells = []
            for chunk in (align_mora.kana_to_mora(r.replace(" ", "")) if " " not in r
                          else [c if c != " " else None for c in r]):
                cells.append(None if chunk is None else {"kana": chunk, "roman": align_mora._ROMA.get(chunk, "")})
            grid.append(cells)
        out.append({"section": sec, "rows": grid})
    return out


# ---------------------------------------------------------------- 内置词表 / 难点音
# 最小对：levels 里 1 = 高拍。声调随程序打包，不联网查辞典。
WORDS = [
    {"a": {"kanji": "橋", "kana": ["は", "し"], "levels": [0, 1], "gloss": "桥"},
     "b": {"kanji": "箸", "kana": ["は", "し"], "levels": [1, 0], "gloss": "筷子"}},
    {"a": {"kanji": "雨", "kana": ["あ", "め"], "levels": [1, 0], "gloss": "雨"},
     "b": {"kanji": "飴", "kana": ["あ", "め"], "levels": [0, 1], "gloss": "糖"}},
    {"a": {"kanji": "神", "kana": ["か", "み"], "levels": [1, 0], "gloss": "神"},
     "b": {"kanji": "紙", "kana": ["か", "み"], "levels": [0, 1], "gloss": "纸"}},
    {"a": {"kanji": "酒", "kana": ["さ", "け"], "levels": [0, 1], "gloss": "酒"},
     "b": {"kanji": "鮭", "kana": ["さ", "け"], "levels": [1, 0], "gloss": "鲑鱼"}},
    {"a": {"kanji": "今", "kana": ["い", "ま"], "levels": [1, 0], "gloss": "现在"},
     "b": {"kanji": "居間", "kana": ["い", "ま"], "levels": [0, 1], "gloss": "起居室"}},
]

# 难点音：按中国语话者的绊倒顺序分组，不是五十音顺。
# 注意：发音要点和例词是交接文档作者写的占位内容，上线前需要母语者过一遍（交接文档 §8 第 1 条）。
SOUNDS = [
    {"key": "tsu", "name": "つ・ず・す", "sounds": [
        {"kana": "つ", "roman": "tsu", "ex": "机（つくえ）", "hint": "舌尖先抵住上齿龈，憋一下再放开，是 t+s 连成一个音，不是汉语的「次」。"},
        {"kana": "ず", "roman": "zu", "ex": "水（みず）", "hint": "つ 的浊音。声带从一开始就振动，别读成「兹」。"},
        {"kana": "す", "roman": "su", "ex": "寿司（すし）", "hint": "只有 s，没有前面的 t。嘴角别咧开。"}]},
    {"key": "long", "name": "長音", "sounds": [
        {"kana": "おー", "roman": "oo", "ex": "お母（かあ）さん", "hint": "长音要占满两拍。心里数两下再换下一个音。"},
        {"kana": "えー", "roman": "ee", "ex": "先生（せんせい）", "hint": "长度不够会被听成另一个词，这是中国人最常丢的一拍。"}]},
    {"key": "soku", "name": "促音", "sounds": [
        {"kana": "っ", "roman": "(stop)", "ex": "切手（きって）", "hint": "促音是一拍的「停」，不是把后面的辅音读重。停住一拍再出声。"}]},
    {"key": "ra", "name": "ら行", "sounds": [
        {"kana": "ら", "roman": "ra", "ex": "来年（らいねん）", "hint": "舌尖轻弹上齿龈一下就走，既不是汉语的 l 也不是卷舌的 r。"},
        {"kana": "り", "roman": "ri", "ex": "料理（りょうり）", "hint": "同样是弹舌，别把舌头卷起来。"}]},
    {"key": "n", "name": "ん", "sounds": [
        {"kana": "ん", "roman": "n", "ex": "日本（にほん）", "hint": "ん 自己占一整拍。后面接什么音，它的口型就跟着变，但长度不变。"}]},
    {"key": "voice", "name": "清濁", "sounds": [
        {"kana": "か / が", "roman": "ka / ga", "ex": "外国（がいこく）", "hint": "浊音从出声那一刻声带就振动。清音不送气，别读成汉语的「卡」。"},
        {"kana": "た / だ", "roman": "ta / da", "ex": "大学（だいがく）", "hint": "同上。中国人常把浊音读成不送气清音，日本人听得出来。"}]},
]

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
                return self.send_json({"history": history_read(q["ref"][0] if "ref" in q else None)})
            if u.path == "/api/article":
                with open(safe(q["path"][0]), encoding="utf-8") as f:
                    return self.send_json({"text": f.read()})
            if u.path == "/api/wordlist":
                return self.send_json({"words": WORDS})
            if u.path == "/api/sounds":
                return self.send_json({"groups": SOUNDS, "gojuon": gojuon(), "align": align_mora.available()})
            if u.path == "/api/baseline":
                if not align_mora.available():
                    return self.send_json({"confidence": None})
                return self.send_json({"confidence": baseline_conf(q["kana"][0])})
            if u.path == "/api/records":
                return self.send_json({"history": history_read(), "summary": history_summary()})
            if u.path == "/api/say":
                return self.send_json({"path": say_ref(
                    q["text"][0], q.get("voice", ["Kyoko"])[0], int(q.get("rate", ["170"])[0]),
                    float(q.get("tempo", ["1"])[0]), float(q.get("pad", ["0"])[0]))})
            if u.path == "/api/target":
                return self.send_json(target_levels(safe(q["ref"][0])))
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
                history_append({**{k: v for k, v in r.items() if k not in ("ref_curve", "my_curve", "t")},
                                "type": "sentence"})
                return self.send_json(r)

            if u.path == "/api/pron":
                if not align_mora.available():
                    raise RuntimeError("没装 torch/torchaudio，模式① / ② 需要它来把录音对到假名上")
                n = int(self.headers.get("Content-Length", 0))
                wav = save_upload(self.headers.get("Content-Type", ""), self.rfile.read(n))
                kind = q.get("type", ["sound"])[0]
                kana = q["kana"][0]
                r = analyze_pron(wav, kana)
                r["recording"] = wav
                r["kana_text"] = kana
                r["type"] = kind
                r["ts"] = time.time()
                r["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                if kind == "sound":
                    r["group"] = q.get("group", [""])[0]
                    r["ok"] = bool(r["confidence"] >= 0.60)
                else:
                    want = [int(x) for x in q.get("levels", [""])[0].split(",") if x != ""]
                    got = r["levels"]
                    r["want"] = want
                    r["word"] = q.get("word", [""])[0]
                    r["ok"] = bool(len(want) == len(got) and all(
                        g is not None and g == w for g, w in zip(got, want)))
                    r["wrong"] = [i for i, (g, w) in enumerate(zip(got, want)) if g is None or g != w] \
                        if len(want) == len(got) else []
                history_append({k: v for k, v in r.items() if k != "mora"})
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
