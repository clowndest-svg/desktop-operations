# 一键为买家生成交付包（PC 便携 exe + 手机 apk + 交付文档，合并成一个 zip）。
# 这是 scripts/deliver_release.ps1 的薄封装：只负责「算下一轮 + 把输出指到交付目录」，
# 真正的构建/落引擎/压包/独立全 CRC 校验/新 exe 自检（ok+frozen+轮次）全交给那条已验证的流水线。
# 不要在这里另起一套打包逻辑 —— 那等于把刚验证过的东西重新发明、把风险引回来。
[CmdletBinding()]
param(
    [string]$Repo = 'E:\DaiMa\AILiaoTianXiangMu',
    [string]$BuildRoot = 'D:\AI\XiaoYe\build\buyer',
    [string]$MobileRoot = 'D:\AI\XiaoYe\build\mobile',
    [string]$EngineRoot = 'D:\AI\XiaoYe\engine',
    [string]$Out = '',
    [switch]$DryRun,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'

$DefaultOut = 'D:\AI\闲鱼展示文档'
if (-not $Out) {
    $Out = $DefaultOut
}
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
$buildCache = Join-Path $BuildRoot '_cache'
foreach ($cacheSetting in @{
    TEMP='tmp'; TMP='tmp'; PYINSTALLER_CONFIG_DIR='pyinstaller'; MPLCONFIGDIR='matplotlib'; NUMBA_CACHE_DIR='numba'
}.GetEnumerator()) {
    $cachePath = Join-Path $buildCache $cacheSetting.Value
    New-Item -ItemType Directory -Path $cachePath -Force | Out-Null
    [Environment]::SetEnvironmentVariable($cacheSetting.Key, $cachePath, 'Process')
}

function Step($m) { Write-Host ("→ " + $m) -ForegroundColor Cyan }
function Ok($m) { Write-Host ("✓ " + $m) -ForegroundColor Green }
function Bad($m) { Write-Host ("✗ " + $m) -ForegroundColor Red }

# ---- 前置检查：少一个都不能往下走，否则半路失败会留下半成品交付物 ----
$deliver = Join-Path $Repo 'scripts\deliver_release.ps1'
foreach ($pair in @(
    @{n='项目仓库'; p=$Repo; dir=$true},
    @{n='交付脚本 deliver_release.ps1'; p=$deliver; dir=$false},
    @{n='手机 apk'; p=$MobileRoot; dir=$true},
    @{n='本地引擎根'; p=$EngineRoot; dir=$true}
)) {
    $exists = if ($pair.dir) { Test-Path -LiteralPath $pair.p -PathType Container } else { Test-Path -LiteralPath $pair.p -PathType Leaf }
    if (-not $exists) { Bad ("缺少 " + $pair.n + "：" + $pair.p); exit 2 }
}
New-Item -ItemType Directory -Path $Out -Force | Out-Null
Ok "交付目录就绪：$Out"

# ---- 下一轮 rNN：扫构建根里已有的 r*，取最大 +1（没有就从 r1 起）----
$nums = Get-ChildItem -LiteralPath @($BuildRoot, (Split-Path -Parent $BuildRoot)) -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^r[0-9]+$' } |
    ForEach-Object { [int]$_.Name.Substring(1) }
$next = 1
if ($nums) { $next = ([int]($nums | Measure-Object -Maximum).Maximum) + 1 }
$round = 'r' + $next
Step ("本轮 = " + $round + "（构建根里最大轮 +1；从当前工作树源码重建）")

if ($DryRun) {
    # 干跑只做「这一轮能不能开建」的体检：新轮还没 exe，不能拿 deliver_release -DryRun 去查它。
    $mobile = $MobileRoot
    $mdirs = Get-ChildItem -LiteralPath $mobile -Directory -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^r[0-9]+$' -and (Get-ChildItem -LiteralPath $_.FullName -Filter '小夜-*.apk' -File)
    }
    if (-not $mdirs) { Bad '没有可用的手机 apk（mobile\rNN\小夜-*.apk）'; exit 2 }
    $apks = @(Get-ChildItem -LiteralPath ($mdirs[0].FullName) -Filter '小夜-*.apk' -File)
    if ($apks.Count -ne 1) { Bad ('最新手机轮里 apk 不唯一：' + ($apks.Name -join '、')); exit 2 }
    Ok ('手机 apk 就绪：' + $apks[0].Name)
    Ok ("预检通过：下一轮 $round 可以从当前源码构建并打包到 $Out（真正构建请去掉 -DryRun）")
    return
}

# ---- 交给已验证流水线：前端构建 → PyInstaller → 落模型/引擎 → 新 exe 自检 → 压合并包 → 独立全 CRC 校验 ----
Step '开始构建、压包与完整回读（本机完整流程约 40 分钟；期间请保持源码不变，并关闭交付目录里的程序）'
$python=Join-Path $Repo '.venv\Scripts\python.exe'
$pending=@(& git -C $Repo diff --name-only HEAD -- jarvis frontend packaging scripts pyproject.toml)
$newFiles=@(& git -C $Repo ls-files --others --exclude-standard -- jarvis frontend/src packaging scripts)
if ($LASTEXITCODE -ne 0 -or $pending.Count -gt 0 -or $newFiles.Count -gt 0) { throw '功能源码尚未提交，请提交并通过门禁后再生成买家包，避免混版发货' }
& $python (Join-Path $PSScriptRoot 'run_gates.py') --frontend --out (Join-Path $BuildRoot ('gates-'+$round+'.json'))
if ($LASTEXITCODE -ne 0) { throw '质量门禁未通过，没有开始买家构建' }
& (Join-Path $PSScriptRoot 'setup_webview_runtime.ps1')
& $deliver -Round $round -Root $BuildRoot -MobileRoot $MobileRoot -Out $Out -MediaEngineRoot $EngineRoot -Edition buyer -Build
$code = $LASTEXITCODE
if ($code -ne 0) { Bad ("打包失败，退出码 " + $code + "（交付目录里不会留下半成品 zip）"); exit $code }

$zip = Join-Path $Out ('小夜-电脑+手机-' + $round + '.zip')
$report = Join-Path $Out ('校验-' + $round + '.json')
Ok ("交付包已生成：" + $zip)
Ok ("校验凭据：" + $report)
Write-Host ''
Write-Host '买家侧提醒（bat 变不了魔术，这几条决定"双击就能用"）：' -ForegroundColor Yellow
Write-Host '  · PC 完整解压到非系统盘，双击 小夜\小夜.exe；买家包已附带 WebView2，无需另外下载。'
Write-Host '  · 首次运行 SmartScreen 可能拦未签名 exe，需点「仍要运行」。'
Write-Host '  · 本地生图/视频要 NVIDIA 显卡+驱动；没有也能跑，那两项会如实提示不可用或走云端。'
if (Test-Path -LiteralPath $report) { Start-Process explorer.exe -ArgumentList ('/select,"' + $zip + '"') }
