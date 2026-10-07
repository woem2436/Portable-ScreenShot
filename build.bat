@echo off
setlocal
cd /d %~dp0

echo [1/3] 安装依赖...
python -m pip install --quiet mss pystray pillow pyinstaller

echo [2/3] 自测（会真的按热键截图，请让桌面保持可见）...
python -X utf8 src\screenshot_tool.py --selftest || goto :fail

echo [3/3] 打包单文件 exe...
python -X utf8 -m PyInstaller --noconfirm --onefile --windowed --name PortableScreenshot ^
  --icon "%~dp0assets\app.ico" --hidden-import pystray._win32 ^
  --distpath dist --workpath build\work --specpath build || goto :fail

echo.
echo 完成：dist\PortableScreenshot.exe（双击运行即可，同目录生成 config.json 与 Screenshots\）
exit /b 0

:fail
echo 构建失败，请查看上面的输出。
exit /b 1
