@echo off
REM 在 Windows 上打包成 exe（在 shadowing 目录下双击运行）
cd /d "%~dp0"
if not exist bin\ffmpeg.exe (
  echo 缺少 bin\ffmpeg.exe：到 https://www.gyan.dev/ffmpeg/builds/ 下载 ffmpeg-release-essentials.zip，
  echo 解压后把 bin\ffmpeg.exe 复制到这里的 bin\ 目录。
  pause & exit /b 1
)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -q praat-parselmouth matplotlib pyinstaller
rmdir /s /q build dist 2>nul
pyinstaller --noconfirm --windowed --name Shadowing --add-data "web;web" --add-binary "bin\ffmpeg.exe;bin" --hidden-import fetch_nhk --collect-all parselmouth app.py
echo.
echo 打包完成: dist\Shadowing\Shadowing.exe  （整个 dist\Shadowing 文件夹一起拷走）
pause
