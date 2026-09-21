#!/usr/bin/env python3
"""把测量结果交给 Claude，换一句有用的教学建议。

分工是有意的：**测量归我们，判断归模型**。

我们这边的测量是验证过的——逐拍对齐、音高、时长、词典调型（和 OJAD 在短句上
一致率 97%）。做不好的一直是最后一步：把一堆数字变成"这次该改哪里、怎么改"。
规则版的「只改一件事」挑的是当次偏差最大的那一拍，实测随机分半只有 14% 选出
同一拍——因为常有四五拍偏得一样多，规则没有办法判断哪个在语言学上更要紧。

模型不听音频（Claude 不支持音频输入），只看数字和目标调型。所以 system 里写明
了每个数字怎么来的、哪些可信、哪些不可信，避免它替我们脑补。

没装 anthropic 或没配 key 时 available() 返回 False，调用方退回规则版。
"""
import json
import os

MODEL = "claude-opus-5"

SYSTEM = """\
你是日语发音教练，学生的母语是中文，正在做跟读练习（shadowing）——模仿 NHK 播音员。

你拿到的是测量数据，不是音频。请只依据数据判断，不要脑补听感。

数据的含义和可靠性：

- 目标调型：查 OpenJTalk 调型词典得到的每拍高低。短句上和 OJAD（东大的权威参照）
  一致率 97%，长句降到 69%——所以句子长时，目标本身就可能有误，别把它当铁律。
- 逐拍「播音员」「我」：该拍的平均音高，单位是半音，相对各自整句的中位数。
  注意这个值受整句下降调影响：句首偏高、句末偏低是正常现象，不等于读错。
  判断某一拍对不对，要看**它和播音员的相对关系**，以及**它和相邻拍的走向**。
- 形状：两条音高曲线对齐后的相关系数，衡量整体走向是否吻合。与音高跨度无关
  （相关系数对缩放不敏感）。
- 幅度：学生的音高跨度 ÷ 播音员的跨度。和形状是**独立**的两件事，不要说
  "把幅度拉大形状就好了"——实测两者相关性只有 -0.13。
- 时长比：该拍时长 ÷ 播音员该拍时长。

输出要求：

- **只说一件最该改的事。** 学生同时改三件事会一件都改不好。
- 指出具体是哪一拍或哪个词，说清楚是偏高还是偏低、该往哪个方向动。
- 给一个能立刻执行的动作，不要给"多听多练"这种话。
- 如果数据显示问题出在整体（比如跨度只有播音员的三分之一），就说整体，
  不要硬挑一拍。
- 用中文。简短，不要客套。
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "focus": {"type": "string", "description": "这次要改的一件事，一句话，20 字以内"},
        "how": {"type": "string", "description": "具体怎么做，可立刻执行，50 字以内"},
        "why": {"type": "string", "description": "为什么是这里，依据哪个数字，40 字以内"},
        "scope": {"type": "string", "enum": ["mora", "word", "whole"],
                  "description": "问题的范围：某一拍 / 某个词 / 整句"},
        "target_mora_index": {"type": ["integer", "null"],
                              "description": "scope 为 mora 时，该拍在逐拍数组里的下标；否则 null"},
    },
    "required": ["focus", "how", "why", "scope", "target_mora_index"],
    "additionalProperties": False,
}


def available() -> bool:
    """装了 SDK 且有凭证才算可用。两者缺一，调用方退回规则版反馈。"""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # 也可能是 `ant auth login` 存的 profile，SDK 会自己找；这里只挡明显没配的情况
        if not os.path.exists(os.path.expanduser("~/.config/anthropic")):
            return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def build_payload(result: dict, target: dict, history: list) -> dict:
    """只送模型判断得上的东西，不要把整个 result 倒过去。"""
    mora = result.get("mora") or []
    # 送假名序列而不是标题：模型要据此分词、判断助词和复合词边界
    kana = "".join(target.get("kana", []))
    return {
        "这句的假名": kana,
        "出处": target.get("text") or "",
        "目标调型": "".join(
            f"{k}{'高' if l == 1 else '低' if l == 0 else '?'}"
            for k, l in zip(target.get("kana", []), target.get("levels", []))
        ),
        "这一遍": {
            "形状": result.get("corr"),
            "幅度": result.get("amp"),
            "速度": result.get("ratio"),
            "播音员音高跨度_半音": result.get("ref_span"),
            "我的音高跨度_半音": result.get("my_span"),
        },
        "逐拍": [
            {"i": i, "拍": m.get("mora"), "播音员": m.get("ref_pitch"),
             "我": m.get("my_pitch"), "时长比": m.get("dur_ratio")}
            for i, m in enumerate(mora)
        ],
        "最近几次的形状分": [h.get("corr") for h in history[-6:] if h.get("corr") is not None],
    }


def advise(result: dict, target: dict, history: list, timeout: float = 20.0) -> dict | None:
    """返回 {focus, how, why, scope, target_mora_index}，失败时返回 None。

    失败一律吞掉返回 None——反馈拿不到就退回规则版，不该让一次网络抖动
    把整个录音结果卡住。
    """
    if not available():
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=timeout, max_retries=1)
        payload = build_payload(result, target, history)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False,
                                                             separators=(",", ":"))}],
        )
        for block in msg.content:
            if block.type == "text":
                return json.loads(block.text)
    except Exception:
        return None
    return None


def main():
    """离线试跑：拿 history.jsonl 里最后一遍真实录音问一次。

        python tutor.py [参考句路径]
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import app
    if not available():
        sys.exit("没有可用凭证或没装 anthropic：pip install anthropic，并设置 ANTHROPIC_API_KEY")
    ref = sys.argv[1] if len(sys.argv) > 1 else None
    if not ref:
        segs = app.list_segments()
        if not segs:
            sys.exit("segments/ 里没有参考句")
        ref = segs[0]["path"]
    hist = [e for e in app.history_read(ref) if e.get("mora")]
    if not hist:
        sys.exit(f"{ref} 还没有带逐拍数据的练习记录")
    tgt = app.target_levels(ref)
    meta = json.load(open(ref + ".json", encoding="utf-8"))
    tgt["text"] = meta.get("label", "")
    print(f"参考句: {ref}")
    print(f"最后一遍: 形状 {hist[-1].get('corr')}  幅度 {hist[-1].get('amp')}  速度 {hist[-1].get('ratio')}")
    print()
    out = advise(hist[-1], tgt, hist)
    if not out:
        sys.exit("调用失败（凭证、网络或 SDK 版本）")
    print(f"  改这个 : {out['focus']}")
    print(f"  怎么做 : {out['how']}")
    print(f"  为什么 : {out['why']}")
    print(f"  范围   : {out['scope']}  拍下标 {out['target_mora_index']}")


if __name__ == "__main__":
    main()
