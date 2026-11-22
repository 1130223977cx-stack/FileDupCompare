@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onedir --windowed --name FileDupCompare --icon app.ico main.py
echo.
echo 打包完成，产物在 dist\FileDupCompare\
pause
