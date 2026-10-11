# Verified release cleanup. Dry-run unless -Apply is explicit. File operations stay in PowerShell.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$Out,
    [Parameter(Mandatory=$true)][ValidatePattern('^r[0-9]+$')][string]$KeepRound,
    [Parameter(Mandatory=$true)][ValidatePattern('^r[0-9]+$')][string]$KeepMobileRound,
    [Parameter(Mandatory=$true)][string]$Verification,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
function Assert-Child([string]$Path, [string]$Base) {
    $basePath = (Resolve-Path -LiteralPath $Base).ProviderPath.TrimEnd('\')
    $resolved = (Resolve-Path -LiteralPath $Path).ProviderPath
    if (-not $resolved.StartsWith($basePath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝根目录或越界路径：$resolved（范围 $basePath）"
    }
    $cursor = Get-Item -LiteralPath $resolved -Force
    while ($null -ne $cursor -and $cursor.FullName -ne $basePath) {
        if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "拒绝链接/联接：$($cursor.FullName)" }
        $cursor = if ($cursor.PSIsContainer) { $cursor.Parent } else { $cursor.Directory }
    }
    return $resolved
}
function File-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}
$rootPath = (Resolve-Path -LiteralPath $Root).ProviderPath
$outPath = (Resolve-Path -LiteralPath $Out).ProviderPath
if ((Get-Item -LiteralPath $rootPath).Parent -eq $null) { throw '不能把磁盘根目录作为构建目录' }
$proof = Get-Content -LiteralPath $Verification -Raw -Encoding UTF8 | ConvertFrom-Json
if ($proof.ok -ne $true -or $proof.full_crc -ne $true -or $proof.round -ne $KeepRound) { throw '新包没有独立完整校验通过的凭据' }
$zip = Assert-Child $proof.zip $outPath
if ((File-Sha256 $zip) -ne $proof.sha256) { throw '新包在校验后发生了变化，禁止清理旧包' }
$keep = Assert-Child (Join-Path $rootPath $KeepRound) $rootPath
$expectedSource = Join-Path $keep 'dist\小夜'
if ([IO.Path]::GetFullPath($proof.source) -ne [IO.Path]::GetFullPath($expectedSource)) { throw '校验凭据对应的不是当前保留的电脑包' }
if (-not (Test-Path -LiteralPath (Join-Path $expectedSource '小夜.exe') -PathType Leaf)) { throw '新电脑包不存在' }
$mobileRoot = Join-Path $rootPath 'mobile'
$mobileKeep = Assert-Child (Join-Path $mobileRoot $KeepMobileRound) $mobileRoot
if (-not (Get-ChildItem -LiteralPath $mobileKeep -Filter '*.apk' -File)) { throw '新手机包不存在' }
$keptExe = Join-Path $expectedSource '小夜.exe'
if ((File-Sha256 $keptExe) -ne $proof.exe_sha256) { throw '新 exe 与完整校验结果不一致' }
$keptApk = Assert-Child $proof.apk $mobileKeep
if ((File-Sha256 $keptApk) -ne $proof.apk_sha256) { throw '新手机包与完整校验结果不一致' }
$pcNumber = [int]$KeepRound.Substring(1)
$mobileNumber = [int]$KeepMobileRound.Substring(1)
$candidates = @()
foreach ($dir in (Get-ChildItem -LiteralPath $rootPath -Directory)) {
    if ($dir.Name -match '^r([0-9]+)$' -and [int]$Matches[1] -lt $pcNumber -and
        (Test-Path -LiteralPath (Join-Path $dir.FullName 'dist\小夜\小夜.exe') -PathType Leaf)) { $candidates += $dir }
}
foreach ($dir in (Get-ChildItem -LiteralPath $mobileRoot -Directory)) {
    if ($dir.Name -match '^r([0-9]+)$' -and [int]$Matches[1] -lt $mobileNumber -and
        (Get-ChildItem -LiteralPath $dir.FullName -Filter '*.apk' -File)) { $candidates += $dir }
}
foreach ($file in (Get-ChildItem -LiteralPath $outPath -File)) {
    # 校验凭据和清理回执以前不在这里：旧轮次的 zip 删了，校验-rNN.json 却留在盘上，
    # 于是「这一轮交付过」还剩一份看起来像凭据的东西 —— 它记的是已经不存在的那个包。
    if ($file.Name -match '^(?:小夜-(?:电脑\+手机-)?|清单-|校验-|清理-)(r[0-9]+)\.(?:zip|txt|json)$' -and
        [int]$Matches[1].Substring(1) -lt $pcNumber) { $candidates += $file }
}
$processes = @([Diagnostics.Process]::GetProcesses() | ForEach-Object { try { $_.MainModule.FileName } catch { } } | Where-Object { $_ })
$removed = @(); $planned = @(); $skipped = @()
foreach ($candidate in $candidates) {
    $base = if ($candidate.FullName.StartsWith($outPath + '\', [StringComparison]::OrdinalIgnoreCase)) { $outPath } else { $rootPath }
    $target = Assert-Child $candidate.FullName $base
    if ($target -eq $keep -or $target.StartsWith($keep + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $target -eq $mobileKeep -or $target.StartsWith($mobileKeep + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "不能删除新版本：$target" }
    $reason = ''
    if ($candidate.PSIsContainer) {
        $entries = @(Get-ChildItem -LiteralPath $target -Force -Recurse)
        if ($entries | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }) { $reason = '包含链接/联接' }
        if ($entries | Where-Object { $_.FullName -notmatch '\\_internal\\' -and
            ($_.Name -eq '数据' -or $_.Name -eq 'preferences.json' -or $_.Name -eq 'devices.json' -or $_.Extension -in @('.db', '.db-wal', '.db-shm', '.sqlite3', '.key', '.keystore')) }) { $reason = '可能包含用户数据或密钥' }
        if ($processes | Where-Object { $_.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase) }) { $reason = '仍有进程在使用' }
    }
    if ($reason) { $skipped += @{path=$target;reason=$reason}; Write-Output "保留：$target（$reason）"; continue }
    $planned += $target
    Write-Output "已核对绝对清理目标：$target"
    if ($Apply) {
        Remove-Item -LiteralPath $target -Recurse -Force
        if (Test-Path -LiteralPath $target) { throw "清理未完成：$target" }
        $removed += $target
    }
}
$result = @{applied=[bool]$Apply;keep=$keep;keep_mobile=$mobileKeep;planned=@($planned);removed=@($removed);skipped=@($skipped)}
$json = $result | ConvertTo-Json -Depth 6
if ($Apply) { $json | Set-Content -LiteralPath (Join-Path $outPath ('清理-' + $KeepRound + '.json')) -Encoding UTF8 }
Write-Output $json


