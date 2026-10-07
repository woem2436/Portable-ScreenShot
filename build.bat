@echo off
setlocal
cd /d %~dp0

echo [1/3] 安装依赖...
python -m pip install --quiet mss pystray pillow pyinstaller

echo [2/3] 自测（会真的按热键截图，请让桌面保持可见）...
if not exist build mkdir build
python -X utf8 src\screenshot_tool.py --selftest > build\selftest.log 2>&1 || goto :fail
type build\selftest.log

echo [3/3] 打包单文件 exe...
python -X utf8 -m PyInstaller --noconfirm --onefile --windowed --name PortableScreenshot ^
  --icon "%~dp0assets\app.ico" --hidden-import pystray._win32 ^
  --distpath dist --workpath build\work --specpath build || goto :fail

echo.
echo 完成：dist\PortableScreenshot.exe（双击运行即可，同目录生成 config.json 与 Screenshots\）
echo 自测结果已保存在 build\selftest.log
pause
exit /b 0

:fail
echo.
echo 构建已中止，请查看上方的报错输出。
echo 自测的完整结果保存在 build\selftest.log，可以直接打开看。
pause
exit /b 1
