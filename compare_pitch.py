#!/usr/bin/env python3
"""
把「参考音频」和「自己的录音」的音高曲线叠在一张图上，看音高（アクセント）哪里不一致。

用法:
    python compare_pitch.py 参考.mp3 我的录音.m4a
    python compare_pitch.py ref.wav mine.wav -o result.png

依赖: pip install praat-parselmouth matplotlib
      非 wav 格式需要 ffmpeg 转码。

看图方法:
  - 两条线的「形状」是否一致最重要（升降的位置），绝对高度不重要（已按各自中位数归一化）。
  - 下方两行色块是每个音频的响度，可以看出你有没有把长音/促音读短、停顿位置对不对。
  - 图下面会打印: 时长比、音高曲线相关系数（越接近 1 越像）。
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

import warnings

import numpy as np
import parselmouth
import matplotlib

warnings.filterwarnings("ignore", message="Glyph .* missing from font")

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# matplotlib 中文/日文字体（找不到就用默认，只是图上文字变方块，不影响曲线）
for f in ["PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Microsoft YaHei", "Yu Gothic", "Hiragino Sans"]:
    if any(f in x.name for x in matplotlib.font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = f
        break


def ffmpeg_bin() -> str:
    """打包版优先用随包附带的 ffmpeg，否则用系统 PATH 里的。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(base, "bin", "ffmpeg"), os.path.join(base, "bin", "ffmpeg.exe")):
        if os.path.exists(cand):
            return cand
    return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG = ffmpeg_bin()


def to_wav(path: str) -> str:
    if path.lower().endswith(".wav"):
        return path
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", path, "-ac", "1", "-ar", "16000", tmp], check=True)
    return tmp


def load(path: str) -> parselmouth.Sound:
    snd = parselmouth.Sound(to_wav(path))
    snd = parselmouth.praat.call(snd, "Filter (pass Hann band)", 80, 0, 20)  # 去低频嗡嗡声
    snd.scale_peak(0.9)
    return snd


def analyze(snd: parselmouth.Sound):
    """返回 (帧时间, 音高Hz(无声=nan), 响度dB)，帧间隔 10ms。"""
    pitch = snd.to_pitch(time_step=0.01, pitch_floor=70, pitch_ceiling=500)
    t = pitch.xs()
    f0 = pitch.selected_array["frequency"].astype(float)
    f0[f0 == 0] = np.nan
    inten = snd.to_intensity(time_step=0.01)
    db = np.interp(t, inten.xs(), inten.values[0])
    return t, f0, db


def find_speech(t, f0, db, label: str):
    """用「有声带振动 + 够响」找语音段，合并小间隙，返回最长一段的 (起, 止)。"""
    fin = db[np.isfinite(db)]
    floor, top = np.percentile(fin, 10), np.percentile(fin, 95)
    if top - floor < 32:
        print(f"[提示] {label} 底噪偏高（说话只比安静时响 {top - floor:.0f} dB）。"
              "靠近麦克风、把系统输入音量拉高，曲线会干净很多。")
    voiced = ~np.isnan(f0) & (db > floor + (top - floor) * 0.35)
    segs, i = [], 0
    while i < len(voiced):
        if voiced[i]:
            j = i
            while j < len(voiced) and voiced[j]:
                j += 1
            segs.append([t[i], t[j - 1]])
            i = j
        else:
            i += 1
    merged = []
    for a, b in segs:  # 间隙 < 0.35s 视为同一句（辅音、促音都在这个范围内）
        if merged and a - merged[-1][1] < 0.35:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    merged = [(a - 0.05, b + 0.05) for a, b in merged if b - a >= 0.12]
    return merged or [(t[0], t[-1])]


def clean_pitch(semis: np.ndarray) -> np.ndarray:
    """剔除离群点（±12 半音以外）和 <=3 帧的孤立碎片，再做中值平滑。"""
    y = semis.copy()
    y[np.abs(y) > 12] = np.nan
    valid = ~np.isnan(y)
    i = 0
    while i < len(y):
        if valid[i]:
            j = i
            while j < len(y) and valid[j]:
                j += 1
            if j - i <= 3:
                y[i:j] = np.nan
            i = j
        else:
            i += 1
    out = y.copy()
    for k in range(len(y)):
        w = y[max(0, k - 2): k + 3]
        w = w[~np.isnan(w)]
        if not np.isnan(y[k]) and len(w):
            out[k] = np.median(w)
    return out


def _cut(t, f0, db, a, b):
    m = (t >= a) & (t <= b)
    if m.sum() < 5:
        return None
    semis = clean_pitch(12 * np.log2(f0[m] / np.nanmedian(f0[m])))  # 相对各自中位数，抹平嗓音高低
    return (t[m] - t[m][0], semis, db[m], (a, b))


def reference_contour(path: str):
    """参考：从第一段语音到最后一段语音整体算一段（一句话里本来就有停顿）。返回 (contour, 句内最长停顿)。"""
    t, f0, db = analyze(load(path))
    segs = find_speech(t, f0, db, "参考")
    a, b = segs[0][0], segs[-1][1]
    gaps = [segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1)]
    return _cut(t, f0, db, a, b), (max(gaps) if gaps else 0.0)


def take_contours(path: str, ref_dur: float, ref_gap: float):
    """我的录音：只按「比参考句内停顿更长」的间隙切成几遍；时长离谱的片段不算一遍；都不合格就整段算一遍。"""
    t, f0, db = analyze(load(path))
    segs = find_speech(t, f0, db, "我的录音")
    thr = max(0.35, ref_gap + 0.25)
    takes = []
    for a, b in segs:
        if takes and a - takes[-1][1] < thr:
            takes[-1][1] = b
        else:
            takes.append([a, b])
    # 候选：每一遍单独算，再加上相邻两遍合并（防止一句话中间停顿太久被切成两半）
    cands = list(takes) + [[takes[i][0], takes[i + 1][1]] for i in range(len(takes) - 1)]
    if len(takes) > 2:
        cands.append([takes[0][0], takes[-1][1]])  # 全部合并也算一个候选
    good = [(a, b) for a, b in cands if 0.4 <= (b - a) / max(ref_dur, 0.1) <= 2.5]
    if not good:
        good = [(segs[0][0], segs[-1][1])]
    good = sorted(set(good))
    return [c for c in (_cut(t, f0, db, a, b) for a, b in good) if c]


def dtw_path(ref_feat: np.ndarray, my_feat: np.ndarray):
    """经典 DTW，返回 (ref_idx, my_idx) 对应关系。特征矩阵 shape=(帧, 维)。"""
    n, m = len(ref_feat), len(my_feat)
    cost = np.full((n + 1, m + 1), np.inf)
    cost[0, 0] = 0
    d = np.linalg.norm(ref_feat[:, None, :] - my_feat[None, :, :], axis=2)
    for i in range(1, n + 1):
        for j in range(max(1, int(i * m / n) - 60), min(m, int(i * m / n) + 60) + 1):  # 限制带宽，防止乱对
            cost[i, j] = d[i - 1, j - 1] + min(cost[i - 1, j], cost[i, j - 1], cost[i - 1, j - 1])
    i, j, path = n, m, []
    while i > 0 and j > 0:
        path.append((i - 1, j - 1))
        k = np.argmin([cost[i - 1, j - 1], cost[i - 1, j], cost[i, j - 1]])
        i, j = (i - 1, j - 1) if k == 0 else (i - 1, j) if k == 1 else (i, j - 1)
    return path[::-1]


def features(semis, db):
    """DTW 用的特征：音高（无声处用线性插值填补）+ 响度，各自标准化。"""
    x = semis.copy()
    idx = np.arange(len(x))
    good = ~np.isnan(x)
    x = np.interp(idx, idx[good], x[good]) if good.sum() > 1 else np.zeros_like(x)
    dbn = (db - np.nanmean(db)) / (np.nanstd(db) + 1e-6)
    return np.stack([x / 4.0, dbn], axis=1)


def warp_to_ref(ref_t, ref_semis, ref_db, my_semis, my_db):
    """把「我的」曲线按 DTW 对齐到参考的时间轴上。"""
    path = dtw_path(features(ref_semis, ref_db), features(my_semis, my_db))
    warped = np.full(len(ref_t), np.nan)
    warped_db = np.full(len(ref_t), np.nan)
    buckets = {}
    for i, j in path:
        buckets.setdefault(i, []).append(j)
    for i, js in buckets.items():
        vals = my_semis[js]
        vals = vals[~np.isnan(vals)]
        warped[i] = vals.mean() if len(vals) else np.nan
        warped_db[i] = my_db[js].mean()
    return warped, warped_db, path


def correlation(t1, y1, t2, y2) -> float:
    m = ~np.isnan(y1) & ~np.isnan(y2)
    if m.sum() < 10 or y1[m].std() == 0 or y2[m].std() == 0:
        return 0.0
    return float(np.corrcoef(y1[m], y2[m])[0, 1])


def compare(reference: str, mine: str, out: str = "compare.png", ref_mora: list | None = None) -> dict:
    """核心入口：返回指标字典并保存图片。命令行和网页版都调用这个。
    ref_mora: 参考句每一拍的起止（相对参考文件开头，秒），有的话会额外做按拍比较。"""
    (rt, rp, rdb, (ra, rb)), ref_gap = reference_contour(reference)
    mines = take_contours(mine, rt[-1], ref_gap)
    scored = []
    for mt, mp, mdb, (a, b) in mines:
        w, wdb, path = warp_to_ref(rt, rp, rdb, mp, mdb)
        c = correlation(rt, rp, rt, w)
        ratio_ = mt[-1] / rt[-1]
        # 长度离参考太远时相关系数不可信（DTW 能把半句硬拉满），按比例打折后再选最好的一遍
        penalty = min(1.0, ratio_ / 0.7) if ratio_ < 0.7 else min(1.0, 1.5 / ratio_) if ratio_ > 1.5 else 1.0
        scored.append((c, mt, mp, mdb, (a, b), w, wdb, path, c * penalty))
    takes = [{"start": round(a, 2), "end": round(b, 2), "corr": round(c, 2)}
             for c, *_, (a, b), _w, _wdb, _p, _s in scored]
    corr, mt, mp, mdb, (a, b), warped, warped_db, path, _ = max(scored, key=lambda x: x[-1])
    rd, md = rt[-1], mt[-1]

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 8), sharex=True,
                                        gridspec_kw={"height_ratios": [4, 1, 1]})
    ax1.plot(rt, rp, color="#1f77b4", lw=2.5, label=f"参考 {os.path.basename(reference)}")
    ax1.plot(rt, warped, color="#d62728", lw=2, alpha=0.85,
             label=f"我的 {os.path.basename(mine)}（已对齐到参考时间轴）")
    ax1.axhline(0, color="gray", lw=0.5, ls="--")
    ax1.set_ylabel("音高（半音，相对中位数）")
    ax1.legend(loc="upper right")
    ax1.grid(alpha=0.3)
    ax1.set_title("音高曲线对比 — 看形状是否一致（升降位置），不看绝对高低")

    ax2.imshow(np.vstack([rdb, warped_db]), aspect="auto", cmap="Blues", extent=[0, rd, 2, 0])
    ax2.set_yticks([0.5, 1.5])
    ax2.set_yticklabels(["参考", "我的"])
    ax2.set_title("响度（深 = 响，浅 = 停顿/静音；我的已对齐）", fontsize=9)

    ri = np.array([p[0] for p in path]) * 0.01
    mi = np.array([p[1] for p in path]) * 0.01
    ax3.plot(ri, mi, color="#2ca02c", lw=1.5)
    ax3.plot([0, rd], [0, rd], color="gray", lw=0.8, ls="--", label="和播音员同速")
    ax3.set_ylabel("我的时间 (s)")
    ax3.set_xlabel("参考时间 (s)")
    ax3.set_title("节奏：绿线比虚线陡 = 这一段你读得慢；平 = 你读得快", fontsize=9)
    ax3.legend(loc="upper left", fontsize=8)
    ax3.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(out, dpi=200)
    plt.close(fig)

    def span(y):  # 用 5%–95% 分位数算起伏幅度，避免个别毛刺
        v = y[~np.isnan(y)]
        return float(np.percentile(v, 95) - np.percentile(v, 5)) if len(v) else 0.0
    rs, ms = span(rp), span(mp)
    amp = ms / rs if rs else 0.0
    ratio = md / rd
    rd, md, ratio, corr, rs, ms, amp = (float(x) for x in (rd, md, ratio, corr, rs, ms, amp))
    ok = bool(corr >= 0.8 and 0.6 <= amp <= 1.6 and 0.75 <= ratio <= 1.35)

    mora_cmp, mora_err = None, None
    if ref_mora:
        try:
            import align_mora
            if align_mora.available():
                ref_m = [{**m, "start": round(m["start"] - ra, 3), "end": round(m["end"] - ra, 3)} for m in ref_mora]
                take = parselmouth.Sound(to_wav(mine)).extract_part(from_time=a, to_time=b, preserve_times=False)
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
                take.save(tmp, "WAV")
                my_m = align_mora.align(tmp, "".join(m["mora"] for m in ref_mora))
                os.remove(tmp)
                mora_cmp = align_mora.compare_mora(ref_m, my_m, rt, rp, mt, mp)
            else:
                mora_err = "没装 torch/torchaudio"
        except Exception as e:  # 按拍比较是附加功能，失败不影响主结果
            mora_err = str(e)[:200]
    return {
        "mora": mora_cmp, "mora_error": mora_err,
        "ref_dur": round(rd, 2), "my_dur": round(md, 2), "ratio": round(ratio, 2),
        "corr": round(corr, 2), "ref_span": round(rs, 1), "my_span": round(ms, 1), "amp": round(amp, 2),
        "takes": takes, "best_take": {"start": round(a, 2), "end": round(b, 2)},
        "ok": ok, "png": out,
        "ref_too_long": bool(rd > 6.0),
        "ref_curve": [None if np.isnan(v) else round(float(v), 2) for v in rp],
        "my_curve": [None if np.isnan(v) else round(float(v), 2) for v in warped],
        "t": [round(float(v), 3) for v in rt],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("reference")
    ap.add_argument("mine")
    ap.add_argument("-o", "--out", default=None, help="输出图片路径（默认 compare.png）")
    args = ap.parse_args()
    r = compare(args.reference, args.mine, args.out or "compare.png")
    if len(r["takes"]) > 1:
        print(f"[我的录音] 检测到 {len(r['takes'])} 遍：" +
              "，".join(f"第{i}遍({t['start']}–{t['end']}s) {t['corr']:.2f}" for i, t in enumerate(r["takes"], 1)))
        print(f"           用最好的一遍（{r['best_take']['start']}–{r['best_take']['end']}s）画图")
    print(f"时长: 参考 {r['ref_dur']}s，我的 {r['my_dur']}s（比值 {r['ratio']}，>1.15 说明整体偏慢）")
    print(f"形状: 对齐后相关系数 {r['corr']}  （0.8 以上算接近，0.5 以下说明升降位置不对）")
    amp = r["amp"]
    print(f"幅度: 参考起伏 {r['ref_span']} 半音，我的 {r['my_span']} 半音（{amp:.0%}）"
          + ("  ← 起伏太小，方向对了但没做出来" if amp < 0.6 else "  ← 幅度过头了" if amp > 1.6 else ""))
    if r["ok"]:
        print("综合: 这一遍可以了，换下一个语块。")
    print(f"图已保存: {r['png']}")


if __name__ == "__main__":
    sys.exit(main())
