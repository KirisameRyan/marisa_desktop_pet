@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  魔理沙一体化启动器  (NapCat + MaiBot + 桌宠)
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
echo.
echo 按任意键关闭本窗口...
pause >nul
