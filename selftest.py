#!/usr/bin/env python3
"""
离线自检：不联网、不需要 ffmpeg、不需要对齐模型，几秒钟跑完。

    python selftest.py

装完依赖后跑一遍，确认分析流程是通的。全绿再去 app.py。
"""
import os
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SR = 16000
_fail = 0


def check(name: str, ok: bool, detail: str = "", note: str = ""):
    """detail 只在失败时打印；note 总是打印。"""
    global _fail
    tail = note or ("" if ok else detail)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"   {tail}" if tail else ""))
    if not ok:
        _fail += 1


def write_tone(path: str, dur: float = 1.8, pad: float = 0.3):
    """合成一段有抑扬、有音节起伏的假嗓音。够 Praat 当成人声来提音高。"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    f0 = 180 + 60 * np.sin(2 * np.pi * t / dur) + 25 * np.sin(2 * np.pi * 3 * t / dur)
    phase = 2 * np.pi * np.cumsum(f0) / SR
    sig = sum((1.0 / k) * np.sin(k * phase) for k in (1, 2, 3, 4, 5))
    env = sum(np.exp(-((t - (i + 0.5) * dur / 6) ** 2) / (2 * (dur / 24) ** 2)) for i in range(6))
    sig *= 0.25 + 0.75 * (env / env.max())
    sig = 0.7 * sig / np.abs(sig).max()
    out = np.concatenate([np.zeros(int(pad * SR)), sig, np.zeros(int(pad * SR))])
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((out * 32000).astype(np.int16).tobytes())


def test_compare():
    """同一段音频和自己比：三项分数都应该是满分。"""
    import compare_pitch
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "syn.wav")
        png = os.path.join(d, "syn.png")
        write_tone(wav)
        r = compare_pitch.compare(wav, wav, png)
    check("形状 corr == 1.00", r["corr"] == 1.0, f"得到 {r['corr']}")
    check("幅度 amp == 1.00", r["amp"] == 1.0, f"得到 {r['amp']}")
    check("速度 ratio == 1.00", r["ratio"] == 1.0, f"得到 {r['ratio']}")
    check("综合 ok 为真", r["ok"] is True)
    check("画出了对比图", os.path.basename(r["png"]) == "syn.png")


def test_kana():
    """文本 → 假名：NHK 式注音、阿拉伯数字、片假名都要能处理。不需要模型。"""
    import align_mora
    cases = [
        ("台風（たいふう）25号（ごう）", "たいふうにじゅうごごう"),
        ("おはようございます", "おはようございます"),
        ("ニュース", "にゅーす"),
    ]
    for text, want in cases:
        got = align_mora.text_to_kana(text)
        check(f"text_to_kana({text!r})", got == want, f"得到 {got!r}，期望 {want!r}")
    mora = align_mora.kana_to_mora("たいふうにじゅうごごう")   # じゅ 算一拍，所以是 10 不是 11
    check("kana_to_mora 拆出 10 拍", len(mora) == 10, f"得到 {mora}")
    toks = align_mora.mora_to_tokens(["きゃ", "っ", "て", "ー"])
    check("mora_to_tokens 处理拗音/促音/长音", toks == ["kya", "t", "te", "e"], f"得到 {toks}")


def test_optional_align():
    """没装 torch 时必须优雅降级，而不是报错。"""
    import align_mora
    avail = align_mora.available()
    check("align_mora.available() 返回布尔值", isinstance(avail, bool),
          note="已装 torch，按假名功能可用" if avail else "没装 torch，按假名功能会自动隐藏（正常）")


if __name__ == "__main__":
    print("音高对比：")
    test_compare()
    print("文本 → 假名：")
    test_kana()
    print("可选依赖：")
    test_optional_align()
    print()
    if _fail:
        sys.exit(f"{_fail} 项没通过。")
    print("全部通过。")
