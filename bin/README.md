# bin/

打包（`build_mac.sh` / `build_win.bat`）时，把**静态编译**的 ffmpeg 放进这个目录，它会被塞进 .app / .exe 里，
这样别人的电脑上不用另外装 ffmpeg。日常用 `python app.py` 不需要这个目录 —— 那时用的是系统 PATH 里的 ffmpeg。

- macOS（Apple Silicon）：<https://ffmpeg.martin-riedl.de/> 下载 macOS arm64 版，解压后把 `ffmpeg` 放到这里
- macOS（Intel）：<https://evermeet.cx/ffmpeg/> 下载静态版
- 然后：`chmod +x bin/ffmpeg && xattr -d com.apple.quarantine bin/ffmpeg`
- Windows：<https://www.gyan.dev/ffmpeg/builds/> 下载 `ffmpeg-release-essentials.zip`，把 `ffmpeg.exe` 放到这里

不要用 Homebrew 装的那个 ffmpeg —— 它链接了本机的动态库，换台机器就跑不起来。

`bin/ffmpeg` 和 `bin/ffmpeg.exe` 已经写进 `.gitignore`，不会被提交。
