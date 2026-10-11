@echo off
setlocal
chcp 65001 >nul
rem 用法：一键做交付包.bat [rNN] [--no-pause]；不给轮次就重压盘上最新那一版。
set "ROUND=%~1"
set "EXTRA=%~2"
if "%ROUND:~0,1%"=="-" (
  set "EXTRA=%ROUND%"
  set "ROUND=auto"
)
set "MODE=-CleanOld -UpdateShortcut"
if "%ROUND%"=="" set "ROUND=auto"
if "%ROUND%"=="auto" (
  echo 没给轮次：只重压盘上最新那一版 exe（没改代码时用这条）。
) else (
  set "MODE=-Build -CleanOld -UpdateShortcut"
  echo 给了轮次 %ROUND%：先构建前端、再打 exe、再压包校验改快捷方式清旧包。
  echo 打包要几分钟，期间不要再跑 npm run build，否则产物缺 assets 会白屏。
)
echo 用户数据、签名密钥和源模型不属于清理范围；任何校验失败均停止。
echo 随包运行时（llama.cpp / scrcpy）没进产物就直接失败，不做半成品包。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\deliver_release.ps1" -Round "%ROUND%" %MODE%
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo 交付失败，退出码 %RC%。请看上方错误，不要发送失败产物。
if "%EXTRA%"=="--no-pause" goto :done
pause
:done
exit /b %RC%
