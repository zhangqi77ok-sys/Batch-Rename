@echo off
chcp 65001 >nul
echo === 安装依赖 ===
pip install -r requirements.txt pyinstaller
echo.
echo === 打包 exe ===
pyinstaller --onefile --windowed --name BatchRename ^
  --collect-all tkinterdnd2 batch_rename.py
echo.
echo === 完成，产物在 dist\BatchRename.exe ===
pause
