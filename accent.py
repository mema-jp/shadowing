#!/usr/bin/env python3
"""从调型词典查目标高低（アクセント型）。

数据来自 OpenJTalk 内置的 NAIST 日本語辞書（3-clause BSD），通过 pyopenjtalk（MIT）访问。
和「从录音音高猜」的做法相比，这里查到的是词本身的调型：单独读时一样、接助词才分家的
尾高／平板也能区分。实测 10 组教科书级最小对全部命中。

pyopenjtalk 是可选依赖。没装时 available() 返回 False，调用方隐藏目标台阶即可。

    python accent.py 台風25号は、遠い南の海にあって
"""
import difflib
import re
import sys

KATA2HIRA = str.maketrans({chr(c): chr(c - 0x60) for c in range(0x30A1, 0x30F7)})
RUBY = re.compile(r"（[ぁ-ゖー]+）")

# 每个假名的元音。归一化长音写法时要用（とおい / とーい 是同一串音）
_VOWEL = {}
for _row, _v in [("あかさたなはまやらわがざだばぱ", "あ"), ("いきしちにひみりぎじぢびぴ", "い"),
                 ("うくすつぬふむゆるぐずづぶぷ", "う"), ("えけせてねへめれげぜでべぺ", "え"),
                 ("おこそとのほもよろをごぞどぼぽ", "お")]:
    for _c in _row:
        _VOWEL[_c] = _v
# 拗音的小书假名也要给出元音，否则「ちゅう」这种认不出长音（'ちゅ'[-1] 是 'ゅ'）
_VOWEL.update({"ゃ": "あ", "ゅ": "う", "ょ": "お", "ぇ": "え", "ぃ": "い", "ぁ": "あ", "ぉ": "お"})


def available() -> bool:
    try:
        import pyopenjtalk  # noqa: F401
        return True
    except ImportError:
        return False


def strip_ruby(text: str) -> str:
    """去掉 NHK 式的「漢字（かな）」注音。

    必须去：词典要靠汉字判断调型，但注音留着会让 OpenJTalk 把汉字和读音各读一遍
    （实测一篇新闻会从 245 拍膨胀到 361 拍）。
    """
    return RUBY.sub("", text)


def levels_from_acc(acc: int, n: int) -> list:
    """アクセント型 → 逐拍高低。东京方言规则：
    型 0（平板）低高高…／型 1（頭高）高低低…／型 k 第 k 拍之后下降。"""
    if n <= 0:
        return []
    if acc == 0:
        return [0] + [1] * (n - 1)
    if acc == 1:
        return [1] + [0] * (n - 1)
    return [0] + [1] * (acc - 1) + [0] * (n - acc)


def phrases(text: str) -> list:
    """按アクセント句切分：[{kana, moras, acc, levels}]。chain_flag=1 表示并入前一句。"""
    import pyopenjtalk
    import align_mora
    out = []
    for f in pyopenjtalk.run_frontend(strip_ruby(text)):
        kana = f.get("pron", "").replace("’", "").replace("、", "").translate(KATA2HIRA)
        if not kana:
            continue
        if int(f.get("chain_flag", -1)) == 1 and out:
            out[-1]["kana"] += kana
        else:
            out.append({"kana": kana, "acc": int(f.get("acc", 0))})
    for p in out:
        p["moras"] = align_mora.kana_to_mora(p["kana"])
        p["levels"] = levels_from_acc(p["acc"], len(p["moras"]))
    return out


def levels(text: str) -> tuple:
    """整段文本 → (拍列表, 高低列表)。"""
    ms, ls = [], []
    for p in phrases(text):
        ms += p["moras"]
        ls += p["levels"]
    return ms, ls


def normalize(moras: list) -> list:
    """把两套记法归到一种，好让词典的拍和对齐模型的拍能对上。

    对齐模型的拍来自 NHK 注音（とおい、は），OpenJTalk 给的是音素写法（とーい、わ）。
    不归一化只有 23% 对得上，归一化后是 89%。

    长音除了「同元音相连」，还要认 おう→オー 和 えい→エー——日语里这两个极常见
    （こう、とう、せい…），不认的话「中央銀行」会在 ちゅ|う、お|う、こ|う 处断三次。
    两边用的是同一个归一化，所以把「思う」这种并非长音的也一并归并不会造成错配。
    """
    out = []
    for m in moras:
        c = "わ" if m == "は" else "え" if m == "へ" else m
        if out and len(c) == 1 and c in "あいうえお":
            prev = out[-1]
            base = _VOWEL.get(prev[-1]) if prev != "ー" else None
            if base == c or (base == "お" and c == "う") or (base == "え" and c == "い"):
                c = "ー"
        out.append(c)
    return out


def align_to(aligned_moras: list, text: str) -> list:
    """把词典调型迁到「已有时间戳的拍」上。

    返回和 aligned_moras 等长的高低列表；对不上的拍给 None，调用方应当留空而不是瞎画
    （实测一篇 245 拍的新闻能覆盖 89%，剩下 11% 宁可不显示）。
    """
    if not aligned_moras:
        return []
    dict_moras, dict_levels = levels(text)
    a, b = normalize(aligned_moras), normalize(dict_moras)
    out = [None] * len(a)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out[i1 + k] = dict_levels[j1 + k]
    return out


# 母语者的参考值：一篇 NHK 新闻 45 个アクセント句，高拍均值−低拍均值中位 2.6 半音。
# 低于 CLEAR 就算"方向也许对，但没读出高低"；FLAT 以内视为完全没分开。
SEP_CLEAR = 2.0
SEP_FLAT = 0.5


def pitch_fit(pitches: list, levels: list) -> dict:
    """把词典调型当目标，看实测音高分没分得开。取代「逐拍二值化再比对错」。

    二值化在两拍差不到 1 个半音时等于掷硬币（实测「紙」两拍相差 0.0 半音，
    判出来是日语里不存在的"低低"）。这里直接给一个带符号的量：
    sep = 高拍均值 − 低拍均值（半音）。正=走向对，绝对值=分得多开。

    verdict: clear（走向清楚）/ flat_ok（方向对但太平）/ flat（没分开）/ reversed（反了）
    """
    hi = [p for p, l in zip(pitches, levels) if l == 1 and p is not None]
    lo = [p for p, l in zip(pitches, levels) if l == 0 and p is not None]
    if not hi or not lo:
        return {"sep": None, "verdict": "unknown", "n_hi": len(hi), "n_lo": len(lo)}
    sep = sum(hi) / len(hi) - sum(lo) / len(lo)
    verdict = ("clear" if sep >= SEP_CLEAR else
               "flat_ok" if sep > SEP_FLAT else
               "flat" if sep > -SEP_FLAT else "reversed")
    return {"sep": round(sep, 2), "verdict": verdict, "n_hi": len(hi), "n_lo": len(lo)}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if not available():
        sys.exit("没装 pyopenjtalk：pip install pyopenjtalk")
    for p in phrases(" ".join(sys.argv[1:])):
        mark = "".join("高" if x else "低" for x in p["levels"])
        print(f"{''.join(p['moras']):<24}型{p['acc']}  {mark}")


if __name__ == "__main__":
    sys.exit(main())
