@echo off
setlocal EnableExtensions
chcp 65001 >/dev/null

rem 把 %~dp0 末尾的 .. 规范化成真实路径。以前是 set "REPO=%~dp0.."，于是每一层
rem 都带着 "闲鱼展示内容\.."，脚本里拼出来的交付目录也就带着它 —— 能跑，但一旦
rem 某个步骤要比较路径前缀（比如"输出必须在本仓库内"这类检查）就会对不上。
for %%I in ("%~dp0..") do set "REPO=%%~fI"

if not exist "%REPO%\pyproject.toml" (
  echo [失败] 这不像项目根目录：%REPO%
  echo        请把本文件放回「闲鱼展示内容」文件夹里再双击。
  pause
  exit /b 2
)

echo 项目      : %REPO%
echo 交付包输出: %REPO%\闲鱼展示内容\交付包
echo 完整构建约 40 分钟。期间请不要改源码，也不要打开交付目录里的程序。
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scriptsuild_buyer_package.ps1" -Repo "%REPO%" -Out "%REPO%\闲鱼展示内容\交付包"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo [失败] 退出码 %RC%。上面几行就是原因，别发旧包。
  echo        常见原因：功能源码还没提交、门禁没过、交付目录里的程序正开着、
  echo        或者磁盘剩余空间不够（完整包约 17 GB）。
) else (
  echo [完成] 交付包在：%REPO%\闲鱼展示内容\交付包
  echo        发出去之前先核对同目录下的「校验-r*.json」。
  start "" "%REPO%\闲鱼展示内容\交付包"
)
pause
exit /b %RC%
