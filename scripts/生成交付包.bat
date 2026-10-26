@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "E:\DaiMa\AILiaoTianXiangMu\scripts\deliver_release.ps1" -Round auto -Root "E:\DaiMa\XiaoYeBuild" -Out "E:\DaiMa\AILiaoTianXiangMu\闲鱼展示内容\交付包" -UpdateShortcut -CleanOld
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo 交付失败，请检查上方结果。
pause
exit /b %RC%
