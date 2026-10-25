@echo off
setlocal
chcp 65001 >nul
set "REPO=%~dp0.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\build_buyer_package.ps1" -Repo "%REPO%" -Out "%REPO%\闲鱼展示内容\交付包"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo 完整买家包构建失败，请查看上方原因，不要发送旧包。
pause
exit /b %RC%
