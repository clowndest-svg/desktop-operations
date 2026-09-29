@echo off
chcp 65001 >/dev/null
setlocal
cd /d "%~dp0"

rem 双击启动「小夜」桌面 HUD。
rem 额外参数会原样传给程序，例如：  启动小夜.bat -v   （打开 DevTools）
rem 需要语音唤醒时点窗口右上角「启用语音」；麦克风不会自己打开。

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [缺少虚拟环境] 没有找到 %PY%
  echo   先执行：  python -m venv .venv
  echo            .venv\Scripts\python -m pip install -e ".[dev,desktop]"
  echo            .venv\Scripts\python scripts\build_desktop.py
  pause
  exit /b 1
)

if not exist "jarvis\ui\web\index.html" (
  echo [缺少界面产物] 没有找到 jarvis\ui\web\index.html
  echo   先执行：  .venv\Scripts\python scripts\build_desktop.py
  echo   ^(需要 Node.js；产物不入 git，源码检出后必须自己构建一次^)
  pause
  exit /b 1
)

echo 正在启动小夜桌面端...
"%PY%" -m jarvis --desktop --voice %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [退出码 %RC%] 详细原因看日志：
  echo   %LOCALAPPDATA%\Jarvis\logs\jarvis.log
  echo   排查表：docs\desktop-operations.md 第 9 节
  pause
)
exit /b %RC%
