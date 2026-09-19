# 跟读练习 — Japanese Shadowing Practice

**A local tool for practising Japanese shadowing (シャドーイング).**
Pick a sentence read by an NHK News Web Easy announcer, record yourself shadowing it in the
browser, and the tool extracts both pitch contours with Praat, aligns them with DTW, and scores
your *shape*, *range* and *speed*. Optionally it aligns the audio to every kana, so you can see
which mora you pitched too high or held too short. Nothing leaves your machine.

---

## 这是什么

练日语语调（アクセント）的时候，最难的是「我知道自己读得不像，但不知道哪里不像」。

这个工具就做一件事：把播音员的音高曲线和你的音高曲线叠在一起，告诉你**哪个位置该升没升、该降没降**。

![界面截图](docs/screenshot.png)

一次练习大概是这样：

1. 抓一篇 NHK News Web Easy 的新闻（文本 + 播音员朗读）
2. 从里面截一句出来当参考
3. 听几遍，然后按空格在浏览器里录自己跟读（可以连读三四遍，会自动挑最好的一遍）
4. 几秒钟后出结果：**形状**、**幅度**、**速度**三个数，加一张对比图
5. 同一句反复练到三个数都进绿区，再换下一句

如果额外装了对齐模型，结果还会精确到每个假名：「「た」高了 5 个半音」「「っ」只有播音员的 40% 长」。

技术上：音高用 Praat（`praat-parselmouth`）提取，两条曲线用 DTW 对齐（所以你中间停顿、拖长也能对上），
绝对音高按各自中位数归一化（所以嗓音高低不影响分数）。服务器是 Python 标准库的 `http.server`，
只监听 `127.0.0.1`，没有 web 框架也没有前端构建步骤。**新闻、录音、分数全部留在你自己的电脑上。**

---

## 安装

需要 **Python 3.10+** 和 **ffmpeg**。

> 下面的步骤在 **macOS（Apple Silicon，Python 3.13）** 上验证过。顺带写出的 Windows 命令**没有人实际跑过**，
> 仅供参考，见「[已知限制](#已知限制)」。

```bash
# 1. ffmpeg
brew install ffmpeg          # macOS
# Windows: winget install ffmpeg，或从 ffmpeg.org 下载后加进 PATH
ffmpeg -version              # 能打印版本号就行

# 2. 代码和依赖
git clone <这个仓库的地址>
cd shadowing
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. 自检（不联网，几秒钟）
python selftest.py           # 应该全部 OK

# 4. 启动
python app.py
```

终端会打印 `打开浏览器访问 http://localhost:8765`（端口被占用会自动往后找到 8774）。
打开这个地址，第一次录音时浏览器会问是否允许麦克风，点允许。按 `Ctrl+C` 停止。

> **不配 NHK cookie 也能用。** 见下面「没有 NHK cookie 怎么办」。

---

## 每天怎么用

打开页面后，**左边是材料，右边是练习**。

### 左边：准备一句参考

1. 点右上角「抓今天的新闻」（需要先配好 cookie），或者直接在列表里点一篇已经抓过的
2. 正文和播放器出现，下面会自动按停顿切出句子候选，点一个就截好了
3. 想自己划范围：播到句首点「起点 = 现在」，句尾点「终点 = 现在」，微调数字 →「试听这段」→「存为参考」
4. 存过的句子都在「已截好的参考句」里，点一下就切换
5. macOS 上还可以在「自己写一句」里打一句日语，用系统朗读生成参考（声音下拉框列出本机实际装了的日语声音）

**一句一句练，比整篇跟读有效得多。** 参考句超过 6 秒页面会提示你截短一点。

### 右边：录音和结果

- 参考会自动播放；`R` 键重听；勾上「循环播放参考」可以边听边跟
- **空格键**开始录音，读三四遍，再按空格停止
- 几秒后出结果：三个分数 + 可交互的音高曲线 +（展开）含响度和节奏的详细图
- 下面的表格是这一句的历次成绩，点任一行可以回看当时的图和录音

所有文件都在仓库目录里：`news/` 新闻、`segments/` 参考句、`recordings/` 你的录音、
`results/` 对比图、`history.jsonl` 分数流水。不想要了直接删文件夹。

### 也可以纯命令行用

```bash
python fetch_nhk.py                                   # 抓最新 2 篇
python fetch_nhk.py -n 5 --no-audio                   # 抓 5 篇，只要文本
python compare_pitch.py 参考.mp3 我的录音.m4a -o out.png
```

---

## 配置 NHK 抓取（约 2 分钟）

NHK 在 2026 年改版（NHK ONE）之后，正文和音频都要求先在浏览器里同意一次「ご利用にあたって」。
这个脚本**不碰你的浏览器**，cookie 由你自己复制过来：

1. 浏览器打开 <https://news.web.nhk/news/easy/>，弹出同意页就点同意
2. `F12` → **Network** → 刷新页面 → 点任意一条发往 `news.web.nhk` 的请求 → **Request Headers**
   → 复制 `Cookie:` 后面的一整行
3. 在仓库目录下执行 `nano nhk_auth.txt`，把复制的内容粘到**第 1 行**，`Ctrl+O` 回车保存，`Ctrl+X` 退出
   - 不要用 macOS 的「文本编辑」，它默认存富文本，脚本读不了（脚本会检测出来并提示你）
4. 如果之后音频报「缺少 z_at」：`F12` → **Console** → 输入 `localStorage.getItem('z_at')`，
   把结果（**不含引号**）粘到 `nhk_auth.txt` 的**第 2 行**

**`nhk_auth.txt` 是你自己的会话凭证。** 它已经写进 `.gitignore`，但还是提醒一句：不要发给别人、不要提交。

会过期。报 401 / 403 基本都是 cookie 或 z_at 过期了，重做一遍上面四步即可。
NHK 没有公开 API，脚本用的是网页背后的接口（sitemap 找文章、cookie 取正文、mediatoken 换音频 token），
NHK 再改版时可能整个失效。

### 没有 NHK cookie 怎么办

页面照样能用，参考音频有两个来源：

- **macOS**：用左边的「自己写一句」，打一句日语，点「生成参考」，系统朗读会生成音频。
  合成音的音高准确、节奏偏机械，练升降够用，练语感还是真人音频更好。
- **任何系统**：手动放一篇进去。在 `news/` 下建一个日期目录，放一个 `.mp3` 和一个**同名** `.txt`：

  ```
  news/2026-09-18/01_练习.mp3
  news/2026-09-18/01_练习.txt
  ```

  `.txt` 的格式是「第 1 行标题，第 2 行来源链接（可留空），第 3 行空行，之后是正文」：

  ```
  練習の文
  
  
  おはようございます。今日はいい天気ですね。
  ```

  刷新页面，这篇就出现在新闻列表里了。

---

## 可选：按假名（モーラ）反馈

装了之后，结果里每个假名会单独标出「比播音员高/低几个半音」「长度是播音员的百分之几」。

```bash
source .venv/bin/activate
pip install -r requirements-align.txt     # torch + torchaudio + pykakasi，约 2GB
python app.py                             # 重启
```

然后在页面上：选一篇新闻 → 句子列表旁边会出现「**按假名对齐这篇**」→ 点一次。
第一次会下载约 1.2GB 的模型（终端里有进度条），之后每篇 10–60 秒。结果缓存在 `news/日期/xxx.align.json`，
同一篇不用再对齐第二遍。

对齐用的是 Meta 的 MMS 强制对齐模型（`torchaudio.pipelines.MMS_FA`），CPU 就能跑。
文本读音来自 NHK 自带的注音，所以读法是准的。**半透明的假名表示模型对那一拍没把握**
（通常是数字读法或专有名词），别太当真。

命令行单独用：

```bash
python align_mora.py 音频.mp3 "台風（たいふう）25号（ごう）"
```

不装也完全没关系——页面会自动隐藏这部分，其它功能一切正常。

---

## 怎么看分数

三个数，都进绿区就算过了这一句：

| 指标 | 含义 | 合格区间 |
|---|---|---|
| **形状** | 对齐后两条音高曲线的相关系数 | **≥ 0.80** |
| **幅度** | 你的音高起伏是播音员的百分之几 | **60% – 160%** |
| **速度** | 你的时长 ÷ 参考时长 | **0.75× – 1.35×** |

怎么改：

- **形状低**（0.5 以下说明升降位置明显不对）——最重要的一项。对着图看哪一段红线和蓝线反着走，
  单独把那个词读几遍。日语的音高是「词本身自带的」，不是情绪，靠猜是猜不对的。
- **幅度小于 60%**：方向对了但没做出来，读得太平。夸张一点，宁可过头。
- **幅度大于 160%**：起伏过头了，听着像在演。
- **速度大于 1.35**：整体偏慢，通常是每个音节都拖了一点点，而不是某处停顿太久——看节奏图能确认。

### 详细图的三行

- **上图**：蓝线是播音员，红线是你（已经 DTW 对齐到播音员的时间轴上）。
  **只看形状**，绝对高度已按各自中位数归一化，嗓音高低不影响。
- **中图**：响度。深色 = 有声音，浅色 = 停顿。看长音、促音有没有读够长，停顿位置对不对。
- **下图**：节奏。绿线比虚线**陡**的那一段是你读得慢，比虚线**平**的地方是你读快了。

### 其它提示

- 录音里连读了好几遍时，会自动切开、分别打分、用最好的一遍画图，并在结果上方列出每一遍
- 出现「底噪偏高」提示，说明录音环境或输入音量需要改善，这时曲线不可靠，先解决录音再看分数

---

## 打包成双击就能开的程序（可选）

先把**静态编译**的 ffmpeg 放进 `bin/`（下载地址见 [bin/README.md](bin/README.md)）。

**macOS**（作者用这个，测过）：

```bash
bash build_mac.sh            # 得到 dist/跟读练习.app
```

第一次打开要**右键 → 打开**（未签名应用 macOS 会拦一次）。数据放在 .app 旁边的 `ShadowingData/` 文件夹里，
`nhk_auth.txt` 也放那里。程序启动后会自动开浏览器，页面右上角有「退出程序」。

**Windows**：仓库里有 `build_win.bat`，但**从来没有在 Windows 上运行过，不保证能用**。
欢迎试，出了问题请开 issue。

打包只能在对应系统上做（Mac 打 Mac 版，Windows 打 Windows 版）。

---

## 已知限制

- **只在 macOS（Apple Silicon，Python 3.13）上验证过。** Windows 完全没测过——`app.py` 本身应该是跨平台的，
  但打包脚本和某些路径处理都没人试过。Linux 同理。
- **参考音靠 macOS 的 `say` 命令，所以和它相关的功能只在 macOS 上可用**：
  「自己写一句」、以及需要合成参考音的练习模式。其它系统上这些入口会自动隐藏，
  **跟读（比对 NHK 播音员录音）不受影响，那部分是跨平台的**。
  声音下拉框列出的是本机实际安装的日语声音（`say -v '?'` 查到的），程序启动时
  按实测质量挑一个真实存在的当默认，不写死——`say` 遇到没装的声音不会报错，
  会静默退回系统默认，用户根本察觉不到。
  排名是拿 10 组最小对量出来的：**Flo 调型 10/10、平均高低差 4.25 个半音**，
  Kyoko 只有 7/10、1.84。要补装声音去「系统设置 → 辅助功能 → 朗读内容 → 系统声音」。
- NHK 的接口是非公开的，改版就会失效；cookie 和 z_at 会过期，需要定期重新复制。
- DTW 是纯 Python 实现的，参考句超过十几秒会明显变慢。工具本来也是设计给「一句一句练」的，
  超过 6 秒页面会提示你截短。
- 按假名对齐对**数字读法和专有名词**不可靠（`align_mora.py` 里的数字读法是通用读法，不处理量词音变）。
  这种拍会画成半透明，别太信。
- 背景噪音大、离麦克风远的时候音高提取会不准，这时所有分数都没有参考价值。
- 浏览器录音用的是 `MediaRecorder`，各浏览器编码不同，后端统一用 ffmpeg 转成 16kHz 单声道 wav。
  在没有麦克风权限或非 localhost 访问时录音会失败。

---

## 许可

代码是 **MIT**，见 [LICENSE](LICENSE)。

**NHK 的文本和音频不在此列。** `fetch_nhk.py` 抓下来的内容版权属于日本放送協会（NHK），
只是下载到你自己的电脑上供个人语言学习使用，**不得再分发、转载，也不得用于数据集或衍生作品**。
`news/` 目录已经写进 `.gitignore`。

## 致谢

- `fetch_nhk.py` 里 NHK 接口的细节（sitemap、cookie 域、mediatoken 流程）参考了
  [yangguo/nhk-easy-fetcher](https://github.com/yangguo/nhk-easy-fetcher)（MIT）的实测记录
- 音高提取靠 [Praat](https://www.fon.hum.uva.nl/praat/) 和
  [parselmouth](https://github.com/YannickJadoul/Parselmouth)
- 假名级对齐用的是 Meta 的 [MMS](https://github.com/facebookresearch/fairseq/tree/main/examples/mms)
  强制对齐模型，通过 `torchaudio.pipelines.MMS_FA`
- 练习材料来自 [NHK News Web Easy](https://news.web.nhk/news/easy/)
