# Fixed target: inspect -> package -> independent verification -> shortcuts -> cleanup.
[CmdletBinding()]
param(
    [ValidatePattern('^(auto|r[0-9]+)$')][string]$Round='auto',
    [string]$Root='E:\BianChengGongJu\JarvisBuild',
    [string]$Out='',
    [string]$MediaEngineRoot='',
    [string]$MobileRoot='',
    [ValidateSet('author','buyer')][string]$Edition='author',
    [switch]$CleanOld,
    [switch]$UpdateShortcut,
    [switch]$Build,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
if ($Edition -eq 'buyer' -and $UpdateShortcut) { throw '买家构建不修改作者桌面快捷方式，请使用独立作者运行目录' }
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw '缺少项目虚拟环境' }
$Root = (Resolve-Path -LiteralPath $Root).ProviderPath
if (-not $Out) { $Out = Join-Path $repo '闲鱼展示内容\交付包' }
if (-not $MobileRoot) { $MobileRoot = Join-Path $Root 'mobile' }
if ($Build -and $Round -eq 'auto') {
    throw '-Build 要配一个明确的轮次（-Round rNN）：auto 的意思是「拿盘上最新的 exe」，'+
          '而 -Build 的意思是「重新做一个」，两者同时给会静默打出上一轮的代码。'
}
if ($Round -eq 'auto') {
    $choice = Get-ChildItem -LiteralPath $Root -Directory | Where-Object {
        $_.Name -match '^r[0-9]+$' -and (Test-Path -LiteralPath (Join-Path $_.FullName 'dist\小夜\小夜.exe'))
    } | Sort-Object @{Expression={[int]$_.Name.Substring(1)};Descending=$true} | Select-Object -First 1
    if (-not $choice) { throw '没有完整的电脑构建' }
    $Round = $choice.Name
}
$release = Join-Path $Root $Round
$exe = Join-Path $release 'dist\小夜\小夜.exe'
if ($Build) {
    # 顺序不能反：先前端、再 PyInstaller，中间不碰 jarvis\ui\web。
    # 反了的表现代码是「打包退出码 0、只有两条 WARNING、产物里没有 assets」，
    # 窗口起得来但白屏 —— 所以这三步在这里按顺序做完，再进下面那道新鲜度门。
    Write-Output "① 构建前端（$Round）"
    & $python (Join-Path $PSScriptRoot 'build_desktop.py')
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败，没有开始打包' }
    Write-Output "② PyInstaller -> $release\dist"
    New-Item -ItemType Directory -Path $release -Force | Out-Null
    $previousEdition = $env:XIAOYE_BUILD_EDITION
    try {
        $env:XIAOYE_BUILD_EDITION = $Edition
        & $python -m PyInstaller --noconfirm --distpath (Join-Path $release 'dist') `
            --workpath (Join-Path $release 'build') (Join-Path $repo 'packaging\jarvis.spec')
    } finally { $env:XIAOYE_BUILD_EDITION = $previousEdition }
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 失败，交付包未生成' }
    Write-Output '③ 落位随包权重'
    & $python (Join-Path $PSScriptRoot 'place_bundled_model.py') --round $Round --build-root $Root
    if ($LASTEXITCODE -ne 0) { throw '随包权重没放好，不能交付' }
    if ($MediaEngineRoot) {
        & $python (Join-Path $PSScriptRoot 'place_media_engine.py') --round $Round --build-root $Root --engine-root $MediaEngineRoot
        if ($LASTEXITCODE -ne 0) { throw '本地图片/视频引擎没有完整落位' }
    }
    if ($Edition -eq 'buyer') {
        $runtimeManifest='D:\AI\XiaoYe\resources\webview2\runtime.json'
        if (-not (Test-Path -LiteralPath $runtimeManifest)) { throw 'Missing fixed WebView2; run setup_webview_runtime.ps1' }
        $runtime=Get-Content -LiteralPath $runtimeManifest -Raw -Encoding UTF8 | ConvertFrom-Json
        $runtimeTarget=Join-Path (Split-Path -Parent $exe) '运行库\WebView2'
        New-Item -ItemType Directory -Path $runtimeTarget -Force | Out-Null
        Copy-Item -Path (Join-Path $runtime.path '*') -Destination $runtimeTarget -Recurse -Force
        if (-not (Test-Path -LiteralPath (Join-Path $runtimeTarget 'msedgewebview2.exe'))) { throw 'Fixed WebView2 did not land in buyer bundle' }
    }
    # ④ 随包运行时按「文件在不在产物里」核对，不按打包日志判：spec 少一条 datas 时的
    # 表现代码是「PyInstaller 退出码 0、产物里没那个目录、界面上一按就说没带」—— 那是把
    # 假能力发出去。自带模型(r87)和投屏(r91)两条都收在这一道门里。
    Write-Output '④ 核对随包运行时（llama.cpp / scrcpy）'
    $vendor = Join-Path (Split-Path -Parent $exe) '_internal\jarvis\vendor'
    $required = @(
        (Join-Path $vendor 'llama\llama-server.exe'),
        (Join-Path $vendor 'scrcpy\scrcpy.exe'),
        (Join-Path $vendor 'scrcpy\adb.exe'),
        (Join-Path $vendor 'scrcpy\scrcpy-server')
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count) { throw ('随包运行时缺这些文件：' + ($missing -join '；')) }
}
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "不存在：$exe（要现做一轮就加 -Build -Round rNN）"
}
$mobileDir = Get-ChildItem -LiteralPath $MobileRoot -Directory | Where-Object {
    $_.Name -match '^r[0-9]+$' -and (Get-ChildItem -LiteralPath $_.FullName -Filter '小夜-*.apk' -File)
} | Sort-Object @{Expression={[int]$_.Name.Substring(1)};Descending=$true} | Select-Object -First 1
if (-not $mobileDir) { throw '没有手机构建' }
$apks = @(Get-ChildItem -LiteralPath $mobileDir.FullName -Filter '小夜-*.apk' -File)
if ($apks.Count -ne 1) { throw '一轮手机目录只能有一个交付 APK，不能猜哪一个是新包' }
$apk = $apks[0].FullName
$maker = Join-Path $PSScriptRoot 'delivery_package.py'
$report = Join-Path $Out ('校验-' + $Round + '.json')
$zip = Join-Path $Out ('小夜-电脑+手机-' + $Round + '.zip')
Write-Output "本次固定目标：电脑 $Round；手机 $($mobileDir.Name)；$zip"
if ($DryRun) {
    & $python $maker --round $Round --build-root $Root --out $Out --with-mobile --with-docs --apk $apk --dry-run
    if ($LASTEXITCODE -ne 0) { throw '预检失败，没有制作或清理任何包' }
    return
}
$qa = Join-Path $release 'qa'
New-Item -ItemType Directory -Path $qa -Force | Out-Null
$probePath = Join-Path $qa ('self-check-' + [Guid]::NewGuid().ToString('N') + '.json')
$process = Start-Process -FilePath $exe -ArgumentList @('--self-check-json', ('"' + $probePath + '"')) -WindowStyle Hidden -PassThru
if (-not $process.WaitForExit(120000)) { Stop-Process -Id $process.Id -Force; throw '新 exe 自检超时' }
$process.Refresh()
if (-not (Test-Path -LiteralPath $probePath)) { throw '新 exe 未生成自检报告' }
$probe = Get-Content -LiteralPath $probePath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($probe.ok -ne $true -or $probe.frozen -ne $true) { throw '新 exe 自检未通过' }
Copy-Item -LiteralPath $probePath -Destination (Join-Path $qa 'self-check.json') -Force
# 这一版 exe 还得**知道自己是不是这一轮**。`check_update` 拿这一个数回答买家「我这版旧不旧」，
# 而它是打包时从 --distpath 里认出来的：spec 少写一行、或者 --distpath 和 -Round 指到两处，
# 发出去的就是一条永远答「读不出轮次」的路 —— 这条门在这里停，比在买家机器上发现便宜。
$stamped = $probe.build.round
if (-not $stamped) {
    throw ('新 exe 不知道自己是谁（buildinfo 里 round 是空）：' +
           '检查更新会对每个买家说「读不出轮次」。查 packaging/jarvis.spec 的 round_from')
}
if ($stamped -ne $Round) {
    throw ("新 exe 自报 $stamped，而这一轮要的是 $Round —— --distpath 和 -Round 不是同一个目录")
}
Write-Output "自检轮次核对：exe 自报 $stamped = 本轮 $Round"
& $python (Join-Path $PSScriptRoot 'verify_android_release.py') --apk $apk --report (Join-Path $mobileDir.FullName 'release.json')
if ($LASTEXITCODE -ne 0) { throw 'APK 校验失败' }
& $python $maker --round $Round --build-root $Root --out $Out --with-mobile --with-docs --apk $apk --dry-run
if ($LASTEXITCODE -ne 0) { throw '交付预检失败' }
& $python $maker --round $Round --build-root $Root --out $Out --with-mobile --with-docs --apk $apk --report $report
if ($LASTEXITCODE -ne 0) { throw '压包失败，旧包未清理' }
& $python $maker --round $Round --build-root $Root --check $zip --apk $apk --report $report
if ($LASTEXITCODE -ne 0) { throw '独立回读失败，旧包未清理' }
if ($UpdateShortcut) {
    & (Join-Path $PSScriptRoot 'repoint_shortcut.ps1') -Round $Round -Root $Root
    if ($LASTEXITCODE -ne 0) { throw '快捷方式未更新，旧包仍保留' }
}
if ($CleanOld) {
    & (Join-Path $PSScriptRoot 'cleanup_releases.ps1') -Root $Root -Out $Out -KeepRound $Round -KeepMobileRound $mobileDir.Name -Verification $report -Apply
}
Write-Output "交付完成：$zip；校验凭据：$report"
