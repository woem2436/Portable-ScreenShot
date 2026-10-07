@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem Keep this file ASCII-only: cmd reads .bat in chunks and can cut a multi-byte
rem character in half, which corrupts the line that follows it.
rem A running exe locks dist\PortableScreenshot.exe, so PyInstaller cannot replace it.
tasklist /FI "IMAGENAME eq PortableScreenshot.exe" /NH | findstr /c:"PortableScreenshot.exe" >nul
if not errorlevel 1 goto :running

echo [1/3] Installing dependencies...
python -m pip install --quiet mss pystray pillow pyinstaller

echo [2/3] Selftest - it really grabs the screen, keep the desktop visible...
if not exist build mkdir build
python -X utf8 src\screenshot_tool.py --selftest > build\selftest.log 2>&1
if errorlevel 1 goto :fail
type build\selftest.log

echo [3/3] Packaging single-file exe...
rem Never append "|| goto" to a ^-continued command: cmd loses its place in the file.
python -X utf8 -m PyInstaller --noconfirm --onefile --windowed --name PortableScreenshot ^
  --icon "%~dp0assets\app.ico" --hidden-import pystray._win32 ^
  --distpath dist --workpath build\work --specpath build src\screenshot_tool.py
if errorlevel 1 goto :fail

echo.
echo Done: dist\PortableScreenshot.exe
echo config.json and Screenshots\ are created beside the exe on first run.
pause
exit /b 0

:running
echo.
echo PortableScreenshot.exe is still running.
echo Right-click the tray icon, choose Exit, then double-click this script again.
pause
exit /b 1

:fail
echo.
echo Build aborted - read the error above.
echo Full selftest output is saved in build\selftest.log
pause
exit /b 1
