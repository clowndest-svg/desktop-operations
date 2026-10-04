@echo off
rem 切到 UTF-8 代码页，否则下面的中文会显示成乱码。
rem 注意是 >nul 不是 >/dev/null —— 后者是 POSIX 写法，cmd 会把它当成一个
rem 不存在的路径并打印 "The system cannot find the path specified."。
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 双击启动「小夜」桌面 HUD。
rem 额外参数会原样传给程序，例如：  启动小夜.bat -v   （打开 DevTools）
rem 需要语音唤醒时点窗口右上角「启用语音」；麦克风不会自己打开。

rem ---------------------------------------------------------------------------
rem 把数据目录与各类缓存挡在系统盘之外。
rem
rem 这些变量的默认值全部落在 C 盘：数据根在 %LOCALAPPDATA%\Jarvis，模型权重在
rem %USERPROFILE%\.cache 下，pip 缓存在 %LOCALAPPDATA%\pip。光是语音模型就约
rem 900 MB，Playwright 浏览器再加约 150 MB。
rem
rem 只在本进程内设置：如果用户已经自己配过（例如系统环境变量里已有），
rem 下面的 if 不会覆盖它。想永久生效，运行
rem   powershell -ExecutionPolicy Bypass -File scripts\setup_env_paths.ps1 -Apply
rem ---------------------------------------------------------------------------
set "JARVIS_ROOT=E:\BianChengGongJu\JarvisData"
if not defined JARVIS_HOME set "JARVIS_HOME=%JARVIS_ROOT%"
if not defined MODELSCOPE_CACHE set "MODELSCOPE_CACHE=%JARVIS_ROOT%\models\modelscope"
if not defined HF_HOME set "HF_HOME=%JARVIS_ROOT%\models\huggingface"
if not defined TORCH_HOME set "TORCH_HOME=%JARVIS_ROOT%\models\torch"
if not defined PIP_CACHE_DIR set "PIP_CACHE_DIR=%JARVIS_ROOT%\cache\pip"
if not defined PLAYWRIGHT_BROWSERS_PATH set "PLAYWRIGHT_BROWSERS_PATH=%JARVIS_ROOT%\cache\playwright"

rem ---------------------------------------------------------------------------
rem 排障开关：如果窗口打开了但里面一片黑，取消下面这行的注释再试一次。
rem
rem 原因：WebView2 的渲染进程有时起不来（Chromium 沙箱在受限环境里拿不到进程权限），
rem 表现就是「窗口正常、内容全黑、日志里什么都没有」。程序自己会在 25 秒后把排查
rem 步骤打在下面这个控制台里，那段文字比这里更详细。
rem
rem 警告：这会摘掉渲染进程的沙箱保护，**只用于排障**，确认能用之后请把注释加回去。
rem 普通桌面环境不需要它。
rem ---------------------------------------------------------------------------
rem set "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--no-sandbox"

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
echo   数据目录：%JARVIS_HOME%
"%PY%" -m jarvis --desktop --voice %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [退出码 %RC%] 详细原因看日志：
  echo   %JARVIS_HOME%\logs\jarvis.log
  echo   排查表：docs\桌面运维手册.md 第 9 节
  pause
)
exit /b %RC%
