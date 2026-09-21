#!/usr/bin/env python3
"""
抓取 NHK News Web Easy 最新新闻（文本 + 朗读音频）。适配 2026 年 NHK ONE 改版后的流程。
只用 Python 标准库；下载音频需要 ffmpeg。

第一次使用（配置 cookie，之后过期了再重做一次）:
  1. 用浏览器打开 https://news.web.nhk/news/easy/ ，出现「ご利用にあたって」就点同意
  2. 按 F12 打开开发者工具 → Network（网络）标签 → 刷新页面
  3. 点任意一条发往 news.web.nhk 的请求 → Request Headers → 找到 Cookie: 那一整行，复制冒号后面的全部内容
  4. 在终端里执行  nano nhk_auth.txt  ，把复制的内容粘到第 1 行，Ctrl+O 回车保存，Ctrl+X 退出
     （不要用 Mac 的"文本编辑"，它默认存富文本）
     如果之后音频报「缺少 z_at」，到 Console 里输入  localStorage.getItem('z_at')  ，把结果（不含引号）粘到第 2 行

用法:
  python fetch_nhk.py           # 最新 2 篇
  python fetch_nhk.py -n 5      # 最新 5 篇
  python fetch_nhk.py --no-audio

输出: news/日期/01_标题.txt 和 .mp3

接口细节（sitemap、cookie 域、mediatoken）参考了 yangguo/nhk-easy-fetcher (MIT) 的实测记录。
NHK 内容仅供个人学习，请勿传播。
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = "https://news.web.nhk"
SITEMAP = f"{BASE}/news/easy/sitemap/sitemap.xml"
TOP_LIST = f"{BASE}/news/easy/top-list.json"
HLS_BASE = "https://media.vd.st.nhk/news/easy_audio"
TOKEN_URL = "https://mediatoken.web.nhk/v1/token"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
AUTH_FILE = "nhk_auth.json"

ARTICLE_RE = re.compile(r"/easy/(?P<id>ne\d{13}|\d{8}de\d+)/(?P=id)\.html$")


# ---------- 网络 ----------
def http_get(url: str, cookie: str = "", extra: dict | None = None) -> bytes:
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "ja,en;q=0.8",
               "Referer": f"{BASE}/news/easy/"}
    if cookie:
        headers["Cookie"] = cookie
    if extra:
        headers.update(extra)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def load_auth() -> tuple[str, str]:
    """读 nhk_auth.txt（第 1 行 cookie，第 2 行可选 z_at）或 nhk_auth.json。"""
    path = next((p for p in ("nhk_auth.txt", AUTH_FILE) if os.path.exists(p)), None)
    if not path:
        return "", ""
    raw = open(path, "rb").read().decode("utf-8-sig", errors="replace").strip()
    if raw.startswith("{\\rtf"):
        sys.exit(f"{path} 是富文本（rtf）格式。Mac 文本编辑请按 Shift+Cmd+T 转成纯文本再保存，"
                 "或者直接在终端里用 nano 创建。")
    cookie = z_at = ""
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            cookie, z_at = d.get("cookie", "").strip(), d.get("z_at", "").strip()
        except json.JSONDecodeError as e:
            sys.exit(f"{path} 不是合法 JSON（{e.msg}）。建议改用 nhk_auth.txt：第 1 行粘 cookie，第 2 行粘 z_at，不要任何引号。")
    else:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        for ln in lines:
            low = ln.lower()
            if low.startswith("cookie:"):
                ln = ln[7:].strip()
            if "=" in ln and ";" in ln and not cookie:
                cookie = ln
            elif not z_at and "=" not in ln:
                z_at = ln
    cookie = cookie.replace("\u201c", "").replace("\u201d", "")
    if not z_at:  # z_at 有时也在 cookie 里
        m = re.search(r"(?:^|;\s*)z_at=([^;]+)", cookie)
        z_at = m.group(1) if m else ""
    if not cookie:
        sys.exit(f"{path} 里没找到 cookie（应该是一长串 a=b; c=d; ... 的形式）。")
    return cookie, z_at


# ---------- 发现文章 ----------
def id_date(aid: str) -> str:
    m = re.match(r"^ne(\d{8})", aid) or re.match(r"^(\d{8})de", aid)
    return m.group(1) if m else "00000000"


def latest_article_urls(n: int) -> list[tuple[str, str]]:
    root = ET.fromstring(http_get(SITEMAP))
    found = {}
    for loc in root.iter():
        if loc.tag.endswith("loc") and loc.text:
            m = ARTICLE_RE.search(loc.text.strip())
            if m:
                found[m.group("id")] = loc.text.strip()
    ids = sorted(found, key=id_date, reverse=True)[:n]
    return [(i, found[i]) for i in ids]


# ---------- 文章正文 ----------
def parse_article(page: str, furigana: bool) -> tuple[str, str]:
    body = re.search(r'<div[^>]+id="js-article-body"[^>]*>(.*?)</div>', page, re.S)
    if not body:
        raise RuntimeError("正文不存在（cookie 未配置或已过期，见脚本顶部说明）")
    title = re.search(r'<h1[^>]*class="article-title"[^>]*>(.*?)</h1>', page, re.S)

    def clean(frag: str) -> str:
        frag = re.sub(r"<rt>(.*?)</rt>", r"（\1）" if furigana else "", frag)
        frag = re.sub(r"<rp>.*?</rp>", "", frag)
        frag = re.sub(r"</p>", "\n", frag)
        frag = re.sub(r"<[^>]+>", "", frag)
        return html.unescape(frag).strip()

    return clean(title.group(1)) if title else "", re.sub(r"\n{3,}", "\n\n", clean(body.group(1)))


def voice_uri(page: str, aid: str, cookie: str) -> str:
    m = re.search(r'"news_easy_voice_uri"\s*:\s*"([^"]+)"', page)
    if m:
        return m.group(1)
    try:  # 备用：top-list.json
        data = json.loads(http_get(TOP_LIST, cookie).decode("utf-8-sig"))
        items = data if isinstance(data, list) else next(
            (v for k, v in data.items() if isinstance(v, list)), [])
        for it in items:
            if aid in (it.get("news_id"), it.get("article_id"), it.get("id")):
                return it.get("news_easy_voice_uri", "")
    except Exception:
        pass
    return ""


# ---------- 音频 ----------
def mint_token(manifest: str, z_at: str) -> str:
    url = TOKEN_URL + "?" + urllib.parse.urlencode({"url": manifest})
    data = json.loads(http_get(url, extra={"Authorization": f"Bearer {z_at}", "Origin": BASE}))
    for d in (data, data.get("data", {}) if isinstance(data, dict) else {}):
        for k in ("hdnts", "token"):
            if isinstance(d, dict) and d.get(k):
                return d[k]
    raise RuntimeError(f"token 响应里没有 hdnts: {data}")


def m3u8_duration(text: str) -> float:
    """播放列表里每个分片的 #EXTINF 之和 = 这段音频应有的时长。主清单没有分片，返回 0。"""
    return sum(float(m) for m in re.findall(r"#EXTINF:([\d.]+)", text))


def media_playlist(man: str, text: str, z_at: str) -> tuple[str, str]:
    """NHK 给的是主清单（只列码率变体），分片在变体清单里。
    不跟进的话 m3u8_duration 恒为 0，完整性校验会变成"没给时长就放行"，等于没验。
    返回 (变体清单地址, 内容)；本来就是媒体清单就原样返回。"""
    if m3u8_duration(text) > 0:
        return man, text
    for ln in text.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            sub = urllib.parse.urljoin(man, s)
            try:
                return sub, http_get(sub + ("&" if "?" in sub else "?") + f"hdnts={mint_token(sub, z_at)}").decode("utf-8")
            except Exception:
                return man, text
    return man, text


def mp3_duration(path: str) -> float:
    """实际下到的时长；文件不存在或不可解码时返回 0。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def audio_ok(out_mp3: str, want: float) -> bool:
    """下全了吗。HLS 掉几个分片时 ffmpeg 常常照样返回 0，只看返回码和文件大小查不出来。"""
    if not os.path.exists(out_mp3) or os.path.getsize(out_mp3) < 10_000:
        return False
    if want <= 0:                       # 播放列表没给时长，只能退回旧判据
        return True
    got = mp3_duration(out_mp3)
    if got < want * 0.98:
        print(f"    音频不完整：应有 {want:.1f}s，只下到 {got:.1f}s")
        return False
    # 能解码到底才算数（截断的文件前半段往往照样能播）
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", out_mp3, "-f", "null", "-"],
                       capture_output=True, text=True)
    if r.stderr.strip():
        print("    音频解码有错：" + r.stderr.strip()[-120:])
        return False
    return True


def download_audio(uri: str, z_at: str, out_mp3: str) -> None:
    stem = os.path.splitext(os.path.basename(uri))[0]
    manifest = f"{HLS_BASE}/{stem}/index.m3u8"
    if not z_at:
        raise RuntimeError("缺少 z_at（见脚本顶部第 4 步）")
    hdnts = mint_token(manifest, z_at)
    tokenized = f"{manifest}?hdnts={hdnts}"

    common = ["ffmpeg", "-y", "-loglevel", "error", "-user_agent", UA,
              "-headers", f"Referer: {BASE}/news/easy/\r\n"]
    text = http_get(tokenized).decode("utf-8")
    manifest, text = media_playlist(tokenized, text, z_at)   # 主清单 → 变体清单，否则拿不到分片时长
    want = m3u8_duration(text)          # 播放列表自带标准答案，用它验收

    # 方式一：直接把带 token 的地址给 ffmpeg
    r = subprocess.run(common + ["-i", tokenized, "-vn", "-c:a", "libmp3lame", "-q:a", "2", out_mp3],
                       capture_output=True, text=True)
    if r.returncode == 0 and audio_ok(out_mp3, want):
        return
    # 方式二：把 m3u8 下载到本地，每个分片地址后面都补上 token
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            s = urllib.parse.urljoin(manifest, s)
            s += ("&" if "?" in s else "?") + f"hdnts={hdnts}"
        lines.append(s)
    local = out_mp3 + ".m3u8"
    with open(local, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    r = subprocess.run(common + ["-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                                 "-i", local, "-vn", "-c:a", "libmp3lame", "-q:a", "2", out_mp3],
                       capture_output=True, text=True)
    os.remove(local)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 失败: " + r.stderr.strip()[-300:])
    if not audio_ok(out_mp3, want):
        raise RuntimeError(f"音频下载不完整（应有 {want:.1f}s，实得 {mp3_duration(out_mp3):.1f}s），"
                           "稍后重试；反复失败多半是 z_at 过期")


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=2, help="抓最新 n 篇（默认 2）")
    ap.add_argument("-d", "--dir", default="news")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--no-furigana", action="store_true")
    args = ap.parse_args()

    cookie, z_at = load_auth()
    if not cookie:
        print(f"没找到 {AUTH_FILE}，只能抓到标题，正文和音频需要 cookie。配置方法见脚本顶部。")

    try:
        articles = latest_article_urls(args.n)
    except Exception as e:
        sys.exit(f"读取 sitemap 失败: {e}")
    if not articles:
        sys.exit("sitemap 里没找到文章，NHK 可能又改了地址格式。")

    for i, (aid, url) in enumerate(articles, 1):
        date = id_date(aid)
        out_dir = os.path.join(args.dir, f"{date[:4]}-{date[4:6]}-{date[6:]}")
        os.makedirs(out_dir, exist_ok=True)
        try:
            page = http_get(url, cookie).decode("utf-8")
            title, text = parse_article(page, furigana=not args.no_furigana)
        except urllib.error.HTTPError as e:
            print(f"[{i}] {aid}: HTTP {e.code}" + ("（cookie 过期，请重新复制）" if e.code in (401, 403) else ""))
            continue
        except Exception as e:
            print(f"[{i}] {aid}: {e}")
            continue

        plain_title = re.sub(r"（[^（）]*）", "", title)  # 文件名不要注音
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", plain_title or aid)[:40]
        base = os.path.join(out_dir, f"{i:02d}_{safe}")
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write(f"{title}\n{url}\n\n{text}\n")
        print(f"[{i}] {title}  → {base}.txt")

        if args.no_audio:
            continue
        uri = voice_uri(page, aid, cookie)
        if not uri:
            print("    没有音频（这篇可能本来就没朗读）")
            continue
        try:
            download_audio(uri, z_at, base + ".mp3")
            print("    音频 OK")
        except urllib.error.HTTPError as e:
            print(f"    音频失败 HTTP {e.code}" + ("（z_at 过期，重新复制）" if e.code in (401, 403) else ""))
        except Exception as e:
            print(f"    音频失败: {e}")


if __name__ == "__main__":
    main()
