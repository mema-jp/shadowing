#!/bin/bash
# 在 Mac 上把跟读练习打包成 .app（在 shadowing 目录下运行: bash build_mac.sh）
set -e
cd "$(dirname "$0")"

if [ ! -x bin/ffmpeg ]; then
  echo "缺少 bin/ffmpeg（要用静态编译版，homebrew 的那个换台机器跑不了）："
  echo "  Apple Silicon: https://ffmpeg.martin-riedl.de/  下载 macOS arm64 的 ffmpeg，解压后把 ffmpeg 放到 bin/"
  echo "  Intel:         https://evermeet.cx/ffmpeg/       下载 ffmpeg 静态版，放到 bin/"
  echo "  然后: chmod +x bin/ffmpeg && xattr -d com.apple.quarantine bin/ffmpeg"
  exit 1
fi

[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q praat-parselmouth matplotlib pyinstaller

rm -rf build dist
pyinstaller --noconfirm --windowed --name "跟读练习" \
  --add-data "web:web" \
  --add-binary "bin/ffmpeg:bin" \
  --hidden-import fetch_nhk \
  --collect-all parselmouth \
  app.py

echo
echo "打包完成: dist/跟读练习.app"
echo "第一次打开: 右键 → 打开（未签名应用 macOS 会拦一次）。数据保存在 .app 旁边的 ShadowingData/ 里。"
