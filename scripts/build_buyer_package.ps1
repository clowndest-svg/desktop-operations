# 一键为买家生成交付包（PC 便携 exe + 手机 apk + 交付文档，合并成一个 zip）。
# 这是 scripts/deliver_release.ps1 的薄封装：只负责「算下一轮 + 把输出指到交付目录」，
# 真正的构建/落引擎/压包/独立全 CRC 校验/新 exe 自检（ok+frozen+轮次）全交给那条已验证的流水线。
# 不要在这里另起一套打包逻辑 —— 那等于把刚验证过的东西重新发明、把风险引回来。
[CmdletBinding()]
param(
    [string]$Repo = 'E:\DaiMa\AILiaoTianXiangMu',
    [string]$BuildRoot = 'D:\AI\XiaoYe\build',
    [string]$EngineRoot = 'D:\AI\XiaoYe\engine',
    [string]$Out = '',
    [switch]$DryRun,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'

$DefaultOut = 'D:\AI\闲鱼展示文档'
if (-not $Out) {
    if ($Force -or $DryRun) {
        $Out = $DefaultOut
    } else {
        $in = Read-Host ("输出目录（直接回车＝默认 $DefaultOut）")
        $Out = if ($in.Trim()) { $in.Trim() } else { $DefaultOut }
    }
}

function Step($m) { Write-Host ("→ " + $m) -ForegroundColor Cyan }
function Ok($m) { Write-Host ("✓ " + $m) -ForegroundColor Green }
function Bad($m) { Write-Host ("✗ " + $m) -ForegroundColor Red }

# ---- 前置检查：少一个都不能往下走，否则半路失败会留下半成品交付物 ----
$deliver = Join-Path $Repo 'scripts\deliver_release.ps1'
foreach ($pair in @(
    @{n='项目仓库'; p=$Repo; dir=$true},
    @{n='交付脚本 deliver_release.ps1'; p=$deliver; dir=$false},
    @{n='构建根（含手机 apk）'; p=(Join-Path $BuildRoot 'mobile'); dir=$true},
    @{n='本地引擎根'; p=$EngineRoot; dir=$true}
)) {
    $exists = if ($pair.dir) { Test-Path -LiteralPath $pair.p -PathType Container } else { Test-Path -LiteralPath $pair.p -PathType Leaf }
    if (-not $exists) { Bad ("缺少 " + $pair.n + "：" + $pair.p); exit 2 }
}
New-Item -ItemType Directory -Path $Out -Force | Out-Null
Ok "交付目录就绪：$Out"

# ---- 下一轮 rNN：扫构建根里已有的 r*，取最大 +1（没有就从 r1 起）----
$nums = Get-ChildItem -LiteralPath $BuildRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^r[0-9]+$' } |
    ForEach-Object { [int]$_.Name.Substring(1) }
$next = 1
if ($nums) { $next = ([int]($nums | Measure-Object -Maximum).Maximum) + 1 }
$round = 'r' + $next
Step ("本轮 = " + $round + "（构建根里最大轮 +1；从当前工作树源码重建）")

if ($DryRun) {
    # 干跑只做「这一轮能不能开建」的体检：新轮还没 exe，不能拿 deliver_release -DryRun 去查它。
    $mobile = Join-Path $BuildRoot 'mobile'
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
if (-not $Force) {
    $ans = Read-Host "确认开始构建并打包 $round？（约 15~20 分钟，输入 Y 继续，其它键取消）"
    if ($ans -ne 'Y' -and $ans -ne 'y') { Write-Host '已取消，未做任何改动。'; return }
}
Step '开始构建并打包（约 15~20 分钟，期间不要改源码、不要开着交付包里的 exe）'
& $deliver -Round $round -Root $BuildRoot -Out $Out -MediaEngineRoot $EngineRoot -Build
$code = $LASTEXITCODE
if ($code -ne 0) { Bad ("打包失败，退出码 " + $code + "（交付目录里不会留下半成品 zip）"); exit $code }

$zip = Join-Path $Out ('小夜-电脑+手机-' + $round + '.zip')
$report = Join-Path $Out ('校验-' + $round + '.json')
Ok ("交付包已生成：" + $zip)
Ok ("校验凭据：" + $report)
Write-Host ''
Write-Host '买家侧提醒（bat 变不了魔术，这几条决定"双击就能用"）：' -ForegroundColor Yellow
Write-Host '  · PC 解压后双击 小夜\小夜.exe；缺 WebView2 运行时的干净系统需先装（可选在包里放离线 bootstrapper）。'
Write-Host '  · 首次运行 SmartScreen 可能拦未签名 exe，需点「仍要运行」。'
Write-Host '  · 本地生图/视频要 NVIDIA 显卡+驱动；没有也能跑，那两项会如实提示不可用或走云端。'
if (Test-Path -LiteralPath $report) { Start-Process explorer.exe -ArgumentList ('/select,"' + $zip + '"') }
