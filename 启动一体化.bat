@echo off
rem ---------------------------------------------------------------
rem  自动提权：MAA 远程控制需要管理员身份。
rem  原因：明日方舟 PC 客户端进程完整性级别高于普通进程，
rem        Windows 的 UIPI 会拦掉所有跨级别窗口消息（MAA 日志里
rem        表现为 "Failed to PostMessageA ... error=5"），
rem        点击会"发出去但落不到游戏上"。提权后即可正常控制。
rem ---------------------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges for MAA control...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  魔理沙一体化启动器  (NapCat + MaiBot + 桌宠)
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
echo.
echo 按任意键关闭本窗口...
pause >nul
