# =============================================================================
# Point JARVIS' data root, model caches and tool caches at a non-system drive.
#
# Why this exists: every one of these defaults lands on C:.
#
#   %LOCALAPPDATA%\Jarvis            data root (database, logs, models, audit)
#   %USERPROFILE%\.cache\huggingface  Hugging Face weights
#   %USERPROFILE%\.cache\torch        TorchScript weights (Silero VAD)
#   %LOCALAPPDATA%\pip\cache          pip's download cache
#   %USERPROFILE%\AppData\...\ms-playwright  Playwright's browser binaries
#
# On this machine the speech models alone are ~900 MB, and a Playwright install
# adds ~150 MB more. A `C:` that is already 75% full is not where they belong.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_env_paths.ps1          # show the plan
#   powershell -ExecutionPolicy Bypass -File scripts\setup_env_paths.ps1 -Apply   # write it
#   powershell -ExecutionPolicy Bypass -File scripts\setup_env_paths.ps1 -Apply -Root "D:\RuanJian\JarvisData"
#
# -Apply writes USER-level environment variables (HKCU\Environment). It never
# touches machine-level variables and never deletes anything: existing data on
# C: has to be moved by hand, and the script prints the exact command.
# =============================================================================

[CmdletBinding()]
param(
    [string]$Root = "E:\BianChengGongJu\JarvisData",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

# Keep every variable inside one root, so "move JARVIS to another disk" stays a
# single edit rather than a hunt through five unrelated folders.
$plan = [ordered]@{
    "JARVIS_HOME"             = $Root
    "MODELSCOPE_CACHE"        = Join-Path $Root "models\modelscope"
    "HF_HOME"                 = Join-Path $Root "models\huggingface"
    "TORCH_HOME"              = Join-Path $Root "models\torch"
    "PIP_CACHE_DIR"           = Join-Path $Root "cache\pip"
    "PLAYWRIGHT_BROWSERS_PATH" = Join-Path $Root "cache\playwright"
}

Write-Host "JARVIS 非系统盘目录规划" -ForegroundColor Cyan
Write-Host "根目录: $Root`n"

foreach ($name in $plan.Keys) {
    $current = [Environment]::GetEnvironmentVariable($name, "User")
    $target = $plan[$name]
    if ([string]::IsNullOrWhiteSpace($current)) {
        Write-Host ("  {0,-26} (未设置) -> {1}" -f $name, $target)
    }
    elseif ($current -eq $target) {
        Write-Host ("  {0,-26} 已指向 {1}" -f $name, $target) -ForegroundColor Green
    }
    else {
        Write-Host ("  {0,-26} 当前 {1}" -f $name, $current) -ForegroundColor Yellow
        Write-Host ("  {0,-26} 将改为 {1}" -f "", $target)
    }
}

if (-not $Apply) {
    Write-Host "`n这只是预览。加上 -Apply 才会写入用户级环境变量。" -ForegroundColor Cyan
    exit 0
}

foreach ($name in $plan.Keys) {
    $target = $plan[$name]
    if (-not (Test-Path -LiteralPath $target)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
    [Environment]::SetEnvironmentVariable($name, $target, "User")
    Write-Host "已设置 $name = $target" -ForegroundColor Green
}

Write-Host "`n完成。新开的终端会生效；当前终端请重启一次。" -ForegroundColor Cyan
Write-Host "提示：如果 C 盘上已经有旧的模型副本，确认新目录可用后再手动删除：" -ForegroundColor Yellow
Write-Host "  %LOCALAPPDATA%\Jarvis\models" -ForegroundColor Yellow
Write-Host "  %USERPROFILE%\.cache\huggingface" -ForegroundColor Yellow
Write-Host "  %USERPROFILE%\.cache\modelscope" -ForegroundColor Yellow
