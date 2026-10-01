# =============================================================================
# Put a 小夜 shortcut on the Desktop (and optionally the Start Menu).
#
# Why a shortcut and not a copied exe: the frozen app is an onedir bundle --
# 小夜.exe sits next to an _internal folder holding ~7400 files and most of the
# 880 MB. Copying just the .exe would produce a shortcut to nothing. A .lnk that
# points at the bundle is the correct shape.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1 -StartMenu
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1 -ExePath "D:\somewhere\小夜.exe"
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1 -Remove
#
# Nothing here is destructive: it creates (or deletes) one .lnk in a folder the
# user can see. The bundle itself is never touched.
# =============================================================================

[CmdletBinding()]
param(
    # 留空 = 自动挑 JarvisBuild 下最新的 dist\小夜\小夜.exe。以前这里写死了一个具体轮次的路径，
    # 于是"跑一下脚本"就把桌面指到那次打包用的产物上；产物一多，图标就停在旧版，而界面上除了
    # 缺功能之外看不出任何区别。写死的默认值迟早会变成错的默认值。
    [string]$ExePath = "",
    [string]$Name = "小夜",
    [switch]$StartMenu,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

# GetFolderPath, not "$env:USERPROFILE\Desktop": on a OneDrive-managed account
# the Desktop is redirected and the literal path does not exist.
$targets = @([Environment]::GetFolderPath("Desktop"))
if ($StartMenu) {
    $targets += Join-Path ([Environment]::GetFolderPath("Programs")) $Name
}

if ($Remove) {
    $removed = 0
    foreach ($folder in $targets) {
        $link = if ($folder -like "*.lnk") { $folder } else { Join-Path $folder "$Name.lnk" }
        if (Test-Path -LiteralPath $link) {
            Remove-Item -LiteralPath $link -Force
            Write-Host "已删除 $link" -ForegroundColor Yellow
            $removed++
        }
    }
    if ($removed -eq 0) { Write-Host "没有找到要删除的快捷方式。" }
    exit 0
}

$root = "E:\BianChengGongJu\JarvisBuild"
if ([string]::IsNullOrWhiteSpace($ExePath)) {
    # Only ever consider a delivered artifact -- <round>\dist\小夜\小夜.exe -- never the
    # intermediate copy under <round>\build\, which is written hours before the one you
    # would actually want to launch.
    $ExePath = Get-ChildItem -LiteralPath $root -Filter "*.exe" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like "*\dist\*\小夜.exe" } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $ExePath) {
        Write-Host "$root 下没有任何 dist 产物，先打包。" -ForegroundColor Red
        exit 1
    }
    Write-Host "未指定 -ExePath，选用最新的产物：" -ForegroundColor Yellow
}
if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Host "找不到可执行文件：$ExePath" -ForegroundColor Red
    Write-Host "先打包：.venv\Scripts\python -m PyInstaller --noconfirm --distpath E:\BianChengGongJu\JarvisBuild\dist --workpath E:\BianChengGongJu\JarvisBuild\build packaging\jarvis.spec" -ForegroundColor Yellow
    exit 1
}

$exe = (Resolve-Path -LiteralPath $ExePath).Path
$working = Split-Path -Parent $exe
$shell = New-Object -ComObject WScript.Shell

foreach ($folder in $targets) {
    if (-not (Test-Path -LiteralPath $folder)) {
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
    }
    $link = if ($folder -like "*.lnk") { $folder } else { Join-Path $folder "$Name.lnk" }
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $exe
    # The working directory matters: the app resolves its data directory from the
    # environment, but a shortcut that starts in C:\Windows would make any
    # relative path in a future config file resolve somewhere surprising.
    $shortcut.WorkingDirectory = $working
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Description = "小夜 —— 本机桌面 AI 助手"
    $shortcut.Save()
    Write-Host "已创建 $link" -ForegroundColor Green
}

Write-Host ""
Write-Host "双击桌面上的「$Name」即可启动，不需要命令行。" -ForegroundColor Cyan
Write-Host "第一次运行 Windows 可能提示「已保护你的电脑」（未签名）：点「更多信息」→「仍要运行」。" -ForegroundColor Yellow
Write-Host "不想要了就删掉那个快捷方式，或者用 -Remove 参数。" -ForegroundColor Cyan
