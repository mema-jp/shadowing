#!/usr/bin/env python3
"""
把一段日语音频和它的文本按「拍（モーラ）」对齐：告诉你每个假名从第几秒到第几秒。
用 Meta 的 MMS 强制对齐模型（torchaudio.pipelines.MMS_FA），CPU 就能跑，第一次运行会下载约 1.2GB 模型。

用法:
    python align_mora.py 音频.mp3 "台風（たいふう）25号（ごう）"      # 文本可以带 NHK 式注音
    python align_mora.py 音频.wav "たいふう" --json out.json

依赖: pip install torch torchaudio pykakasi   （加上原来的 praat-parselmouth）
"""
import argparse
import json
import collections
import os
import re
import sys
import unicodedata

import numpy as np

# ---------------- 文本 → 假名 ----------------
_DIGITS = ["", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]


def number_to_kana(n: int) -> str:
    """整数 → 假名读法（通用读法，不处理量词导致的音变，对齐够用）。"""
    if n == 0:
        return "ぜろ"
    units = [(10 ** 8, "おく"), (10 ** 4, "まん"), (1000, "せん"), (100, "ひゃく"), (10, "じゅう")]
    out = ""
    for base, name in units:
        d = n // base
        n %= base
        if d == 0:
            continue
        if base >= 10 ** 4:
            out += number_to_kana(d) + name
        elif d == 1 and base in (1000, 100, 10):
            out += {1000: "せん", 100: "ひゃく", 10: "じゅう"}[base]
        elif base == 1000:
            out += {3: "さんぜん", 8: "はっせん"}.get(d, _DIGITS[d] + "せん")
        elif base == 100:
            out += {3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"}.get(d, _DIGITS[d] + "ひゃく")
        else:
            out += _DIGITS[d] + name
    return out + _DIGITS[n]


_ALPHA = {"a": "えー", "b": "びー", "c": "しー", "d": "でぃー", "e": "いー", "f": "えふ", "g": "じー", "h": "えいち",
          "i": "あい", "j": "じぇー", "k": "けー", "l": "える", "m": "えむ", "n": "えぬ", "o": "おー", "p": "ぴー",
          "q": "きゅー", "r": "あーる", "s": "えす", "t": "てぃー", "u": "ゆー", "v": "ぶい", "w": "だぶりゅー",
          "x": "えっくす", "y": "わい", "z": "ぜっと"}


def kata_to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def text_to_kana(text: str) -> str:
    """NHK 式「漢字（かな）」→ かな；数字 → 读法；片假名 → 平假名；没有注音的汉字用 pykakasi 猜。"""
    text = re.sub(r"([一-龥々]+)[（(]([ぁ-ゖー]+)[）)]", r"\2", text)  # 先处理注音，再做 NFKC（它会把全角括号变半角）
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\d+", lambda m: number_to_kana(int(m.group())), text)
    text = re.sub(r"[A-Za-z]+", lambda m: "".join(_ALPHA[c] for c in m.group().lower()), text)  # FRB → えふあーるびー
    text = kata_to_hira(text)
    if re.search(r"[一-龥々]", text):  # 还有没注音的汉字
        try:
            import pykakasi
            text = "".join(x["hira"] for x in pykakasi.kakasi().convert(text))
        except ImportError:
            pass
    return "".join(c for c in text if "ぁ" <= c <= "ゖ" or c == "ー")


# ---------------- 假名 → 拍 → 罗马字 ----------------
_SMALL = set("ぁぃぅぇぉゃゅょゎ")
_ROMA = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "ゐ": "i", "ゑ": "e", "を": "o", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o", "ゔ": "vu",
    "きゃ": "kya", "きゅ": "kyu", "きょ": "kyo", "しゃ": "sha", "しゅ": "shu", "しょ": "sho",
    "ちゃ": "cha", "ちゅ": "chu", "ちょ": "cho", "にゃ": "nya", "にゅ": "nyu", "にょ": "nyo",
    "ひゃ": "hya", "ひゅ": "hyu", "ひょ": "hyo", "みゃ": "mya", "みゅ": "myu", "みょ": "myo",
    "りゃ": "rya", "りゅ": "ryu", "りょ": "ryo", "ぎゃ": "gya", "ぎゅ": "gyu", "ぎょ": "gyo",
    "じゃ": "ja", "じゅ": "ju", "じょ": "jo", "びゃ": "bya", "びゅ": "byu", "びょ": "byo",
    "ぴゃ": "pya", "ぴゅ": "pyu", "ぴょ": "pyo", "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe", "ふぉ": "fo",
    "てぃ": "ti", "でぃ": "di", "うぃ": "wi", "うぇ": "we", "うぉ": "wo", "しぇ": "she", "ちぇ": "che", "じぇ": "je",
}


def kana_to_mora(kana: str) -> list:
    mora, i = [], 0
    while i < len(kana):
        c = kana[i]
        if i + 1 < len(kana) and kana[i + 1] in _SMALL and c not in _SMALL and c not in "っんー":
            mora.append(c + kana[i + 1])
            i += 2
        else:
            mora.append(c)
            i += 1
    return mora


def mora_to_tokens(mora: list) -> list:
    """每拍 → 罗马字（MMS 词表只有 a-z）。っ 用下一拍的辅音，ー 用上一拍的元音，ん → n。"""
    out = []
    for i, m in enumerate(mora):
        if m == "っ":
            nxt = _ROMA.get(mora[i + 1], "t") if i + 1 < len(mora) else "t"
            out.append(nxt[0] if nxt[0] not in "aiueo" else "q")
        elif m == "ー":
            prev = out[-1] if out else "a"
            out.append(prev[-1] if prev[-1] in "aiueo" else "a")
        elif m == "う" and out and out[-1][-1] == "o":
            out.append("o")          # おう＝オー，和前一拍是同一个元音
        elif m == "い" and out and out[-1][-1] == "e":
            out.append("e")          # えい＝エー
        else:
            out.append(_ROMA.get(m, "a"))
    return out


# ---------------- 对齐 ----------------
_bundle = None
_model = None


def _load_model():
    global _bundle, _model
    if _model is None:
        import torch
        import torchaudio
        _bundle = torchaudio.pipelines.MMS_FA
        _model = _bundle.get_model(with_star=False)
        _model.eval()
        torch.set_num_threads(max(1, (__import__("os").cpu_count() or 4) // 2))
    return _bundle, _model


def load_audio_16k(path: str) -> np.ndarray:
    """Praat 只认 wav/aiff 一类，.m4a/.aac 要先过一道 ffmpeg。
    统一走 compare_pitch.to_wav，别再直接把路径丢给 parselmouth。"""
    import parselmouth
    import compare_pitch
    wav = compare_pitch.to_wav(path)
    try:
        snd = parselmouth.Sound(wav)
        if snd.n_channels > 1:
            snd = snd.convert_to_mono()
        snd = snd.resample(16000)
        return snd.values[0].astype(np.float32)
    finally:
        if wav != path and os.path.exists(wav):
            os.remove(wav)


def align_tokens(emission, token_ids: list, token_groups: list):
    """emission: (T, C) log-prob；token_ids: 扁平的 token id；token_groups: 每拍占几个 token。
    返回每拍的 (起帧, 止帧, 平均分)。独立出来方便离线测试。"""
    import torch
    import torchaudio.functional as F
    targets = torch.tensor([token_ids], dtype=torch.int32)
    aligned, scores = F.forced_align(emission.unsqueeze(0), targets, blank=0)
    spans = F.merge_tokens(aligned[0], scores[0].exp())
    assert len(spans) == len(token_ids), "对齐结果 token 数不符"
    starts, ends, scores, k = [], [], [], 0
    for n in token_groups:
        seg = spans[k:k + n]
        k += n
        starts.append(seg[0].start)
        ends.append(seg[-1].end)
        scores.append(float(np.mean([s.score for s in seg])))
    # CTC 只在每个音上打一个尖峰，其余帧都是空白，所以「这一拍的结束 = 下一拍的开始」才是真实边界。
    # 最后一拍：用它的尖峰结束 + 其余拍的中位长度来估计。
    bounds = starts[1:] + [None]
    med = float(np.median([b - a for a, b in zip(starts[:-1], starts[1:])])) if len(starts) > 1 else 10
    if bounds[-1] is None:
        bounds[-1] = int(min(emission.shape[0], max(ends[-1], starts[-1] + med)))
    return [(a, b, sc) for a, b, sc in zip(starts, bounds, scores)]


def _trim_pauses(res: list, audio: np.ndarray, sr: int = 16000, win: float = 0.02, rel_db: float = 22.0):
    """「这一拍的结束 = 下一拍的开始」会把句中停顿算进前一拍；用响度把每拍尾部的静音切掉。"""
    hop = int(sr * win)
    n = len(audio) // hop
    rms = np.array([np.sqrt(np.mean(audio[i * hop:(i + 1) * hop] ** 2)) + 1e-9 for i in range(n)])
    db = 20 * np.log10(rms)
    thr = np.percentile(db, 95) - rel_db
    for r in res:
        a, b = int(r["start"] / win), int(r["end"] / win)
        if b - a <= 2:
            continue
        seg = db[a:b]
        loud = np.where(seg > thr)[0]
        if len(loud) and loud[-1] + 1 < len(seg) - 1:
            r["end"] = round(float((a + loud[-1] + 2) * win), 3)
    return res


def align(audio_path: str, text: str) -> list:
    """返回 [{"mora": "た", "start": 0.12, "end": 0.25, "score": 0.93}, ...]，秒为单位。"""
    import torch
    kana = text_to_kana(text)
    mora = kana_to_mora(kana)
    if not mora:
        raise ValueError("文本里没有假名")
    toks = mora_to_tokens(mora)
    bundle, model = _load_model()
    dictionary = bundle.get_dict(star=None)
    # 长音的后半拍和前一拍是同一个元音。CTC 要求相邻的相同 token 之间必须有 blank，
    # 硬分开的结果是后半拍退化成 ~20ms、确信度接近 0——拿一定念了那个词的合成音实测
    # 也是如此，所以那个低分是对齐假象，不代表音频里没有。这里合成一个 token 让它
    # 自然占满整个长音，事后再把跨度均分回两拍。
    ids, groups, owner = [], [], []
    for i, t in enumerate(toks):
        if i and len(t) == 1 and t == toks[i - 1][-1:]:
            owner.append(len(groups) - 1)          # 并入前一拍，不单独成组
            continue
        ids.extend(dictionary[c] for c in t)
        groups.append(len(t))
        owner.append(len(groups) - 1)
    audio = load_audio_16k(audio_path)
    wav = torch.from_numpy(audio).unsqueeze(0)
    with torch.inference_mode():
        emission, _ = model(wav)
    emission = torch.log_softmax(emission[0], dim=-1)
    ratio = wav.shape[1] / emission.shape[0] / 16000  # 每帧多少秒
    spans = align_tokens(emission, ids, groups)
    share = collections.Counter(owner)          # 每个组被几拍共用
    res, used = [], {}
    for m, g in zip(mora, owner):
        a, b, sc = spans[g]
        k = share[g]
        j = used.get(g, 0)
        used[g] = j + 1
        aa, bb = a + (b - a) * j / k, a + (b - a) * (j + 1) / k
        res.append({"mora": m, "start": round(aa * ratio, 3), "end": round(bb * ratio, 3), "score": round(sc, 2)})
    return _trim_pauses(res, audio)


def available() -> bool:
    try:
        import torch  # noqa: F401
        import torchaudio  # noqa: F401
        return True
    except ImportError:
        return False


# 一拍最长按这么多秒算。句尾那一拍常把后面的停顿一起吞进去（实测「す」拿到
# 6.04–7.54s，1.5 秒），用真实中点判断归属就会把它排除在句子之外。超过这个长度
# 的拍一律按起点附近判断。日语一拍约 0.10–0.20s，0.4s 已经很宽。
MAX_MORA = 0.4


def mora_mid(r: dict) -> float:
    """判断某一拍属于哪一段时用的时间点。长拍只看开头那一截。"""
    return (r["start"] + min(r["end"], r["start"] + MAX_MORA)) / 2


def mora_in_range(alignment: list, start: float, end: float) -> list:
    """从整篇对齐结果里取出落在 [start, end] 内的拍，时间改成相对 start。"""
    out = []
    for r in alignment:
        mid = mora_mid(r)
        if start <= mid <= end:
            out.append({**r, "start": round(r["start"] - start, 3), "end": round(r["end"] - start, 3)})
    return out


def compare_mora(ref_mora: list, my_mora: list, ref_t, ref_semis, my_t, my_semis) -> list:
    """按拍比较：每拍的平均音高（半音，相对各自中位数）和长度。ref/my 的 t、semis 来自 compare_pitch。"""
    def mean_pitch(t, semis, a, b):
        m = (t >= a) & (t <= b) & ~np.isnan(semis)
        return float(np.mean(semis[m])) if m.any() else None
    out = []
    for r, m in zip(ref_mora, my_mora):
        rp = mean_pitch(ref_t, ref_semis, r["start"], r["end"])
        mp = mean_pitch(my_t, my_semis, m["start"], m["end"])
        rd, md = r["end"] - r["start"], m["end"] - m["start"]
        out.append({
            "mora": r["mora"],
            "ref_pitch": None if rp is None else round(rp, 1),
            "my_pitch": None if mp is None else round(mp, 1),
            "pitch_diff": None if rp is None or mp is None else round(mp - rp, 1),
            "ref_dur": round(rd, 2), "my_dur": round(md, 2),
            "dur_ratio": round(md / rd, 2) if rd > 0.02 else None,
            "ref_start": r["start"], "ref_end": r["end"],
            "my_start": m["start"], "my_end": m["end"],
            "score": min(r["score"], m["score"]),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("text")
    ap.add_argument("--json", help="把结果存成 JSON")
    args = ap.parse_args()
    res = align(args.audio, args.text)
    print(f"{'拍':<4}{'起':>7}{'止':>7}{'长':>7}{'置信':>6}")
    for r in res:
        print(f"{r['mora']:<4}{r['start']:>7.2f}{r['end']:>7.2f}{r['end'] - r['start']:>7.2f}{r['score']:>6.2f}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print("已保存:", args.json)


if __name__ == "__main__":
    sys.exit(main())
