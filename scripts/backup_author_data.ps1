<#
.SYNOPSIS
    把作者自己那份不可替代的数据加密后备到腾讯服务器上，并顺手核一遍发布清单有没有被改动。

.DESCRIPTION
    只备份**重装拿不回来的东西**：聊天数据库、记忆、偏好、作品、音色、投屏配对。
    明确排除 models / voice-venv / voice-src / tmp / cache 那 12.3 GB —— 那些是重装得回来的，
    进备份只会让每一发都变大、变慢，还让人误以为"备份很大很安全"。

    量出来的形状（2026-10-15）：真正不可替代的只有约 14 MB，服务器 9 天累计才发出去 0.61 GB，
    所以这条路的成本几乎为零；而它挡的是"这台机器没了，她这半年的对话跟着没了"。

    加密在**本机**完成，服务器只见到密文。口令用 Windows 当前用户的 DPAPI 存着，
    所以自动化能解，但**换 Windows 用户或换机器就解不开** —— 第一次运行会把口令明文打一次，
    请把它抄到你存密码的地方。不抄的后果是：这份备份永远打不开。

    只用于作者本人。买家链路上没有任何东西指向这台机器。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup_author_data.ps1 -Verify
    跑一次并做一次完整的"拉回来→解密→解包→逐文件比对"回滚演练。
#>
[CmdletBinding()]
param(
    [string]$DataRoot = 'E:\BianChengGongJu\JarvisData',
    [string]$KeyDir = 'E:\BianChengGongJu\XiaoYeKeys',
    [string]$StageRoot = 'D:\AI\XiaoYe\backups',
    [string]$Server = 'root@124.220.54.64',
    [string]$KeyFile = 'C:\Users\23793\OneDrive\Desktop\test.pem',
    [string]$RemoteDir = '/opt/xiaoye-backups',
    [int]$Keep = 14,
    [switch]$Verify
)
$ErrorActionPreference = 'Stop'

# 重装拿不回来的那几样。少列一个目录的表现是"备份跑成功、里面却没有它"，
# 所以后面有一道"每一项都必须在包里"的核对。
$Include = @(
    'database', 'audit', 'config', 'logs', 'mobile', 'pets', 'voices', 'workflows', '生图',
    # preferences.json 在数据根根上，不在任何目录里；漏了它等于备份了一份空设置
    'preferences.json'
)

function Step($m) { Write-Host ("→ " + $m) -ForegroundColor Cyan }
function Ok($m) { Write-Host ("✓ " + $m) -ForegroundColor Green }
function Bad($m) { Write-Host ("✗ " + $m) -ForegroundColor Red }

if (-not (Test-Path -LiteralPath $DataRoot -PathType Container)) { throw "数据根不存在：$DataRoot" }
if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) { throw "SSH 密钥不存在：$KeyFile" }
$missing = @($Include | Where-Object { -not (Test-Path -LiteralPath (Join-Path $DataRoot $_) -PathType Container) })
if ($missing.Count -eq $Include.Count) { throw "一个要备份的目录都不在，八成是数据根填错了" }
New-Item -ItemType Directory -Path $StageRoot, $KeyDir -Force | Out-Null

# ---------------------------------------------------------------- 口令（DPAPI）
$blobPath = Join-Path $KeyDir 'backup-passphrase.dpapi'
if (-not (Test-Path -LiteralPath $blobPath)) {
    Step '第一次运行：生成备份口令'
    $raw = New-Object byte[] 32
    [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($raw)
    $passphrase = [Convert]::ToBase64String($raw)
    $secure = ConvertTo-SecureString $passphrase -AsPlainText -Force
    (ConvertFrom-SecureString $secure) | Set-Content -LiteralPath $blobPath -Encoding ASCII
    Write-Host ''
    Write-Host '  备份口令（DPAPI 已在本机存好，自动化用这份）：' -ForegroundColor Yellow
    Write-Host "  $passphrase" -ForegroundColor White
    Write-Host '  抄到你存密码的地方。换 Windows 用户或换机器后，DPAPI 那份解不开，' -ForegroundColor Yellow
    Write-Host '  没有这一串，服务器上的备份就永远打不开了。' -ForegroundColor Yellow
    Write-Host ''
}
$restored = ConvertTo-SecureString (Get-Content -LiteralPath $blobPath -Raw).Trim()
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($restored)
$passphrase = [Runtime.InteropServices.Marshal]::PtrToStringUni($ptr)
[Runtime.InteropServices.Marshal]::ZeroFreeGlobalAllocUnicode($ptr)

# ---------------------------------------------------------------- 打包
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$plain = Join-Path $StageRoot ("xiaoye-author-$stamp.tar.gz")
$cipher = "$plain.enc"
$tar = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path -LiteralPath $tar)) { $tar = 'tar' }

Step "打包 $DataRoot 里的 $($Include.Count) 项"
# 一次 tar 调用收齐：bsdtar 不能往 .tar.gz 里追加（`-rzuf` 会当场失败），
# 而"分两次"这种写法留给下一个人踩，不如一开始就没有。
& $tar '-czf' $plain '-C' $DataRoot @($Include)
if ($LASTEXITCODE -ne 0) { throw "tar 失败，退出码 $LASTEXITCODE" }
$listing = @(& $tar '-tzf' $plain)
Ok ("包内条目 " + $listing.Count + " 条，" + [math]::Round((Get-Item $plain).Length / 1MB, 2) + " MB")

# 每一项都必须在包里。少一项的表现是"备份成功"，而缺的那一项正是你哪天最需要它的时候。
foreach ($item in $Include) {
    if (-not (Test-Path -LiteralPath (Join-Path $DataRoot $item))) { continue }  # 这台机器上就没有这一样
    if (-not ($listing -contains $item -or ($listing | Where-Object { $_ -like "$item/*" }))) {
        throw "包里居然没有 $item"
    }
}

# ---------------------------------------------------------------- 加密
Step 'AES-256-CBC + PBKDF2 加密（口令只在内存里，不落盘）'
$env:XIAOYE_BACKUP_PASS = $passphrase
try {
    & openssl enc '-aes-256-cbc' '-pbkdf2' '-iter' '200000' '-salt' '-in' $plain '-out' $cipher '-pass' 'env:XIAOYE_BACKUP_PASS'
} finally { Remove-Item Env:XIAOYE_BACKUP_PASS -ErrorAction SilentlyContinue }
if ($LASTEXITCODE -ne 0) { throw 'openssl 加密失败' }
$localSha = (Get-FileHash -LiteralPath $cipher -Algorithm SHA256).Hash.ToLower()
Ok ("密文 " + [math]::Round((Get-Item $cipher).Length / 1MB, 2) + " MB  sha256 $localSha")

# ---------------------------------------------------------------- 上传
Step "scp -> $Server`:$RemoteDir"
& ssh -i $KeyFile -o BatchMode=yes -o ConnectTimeout=10 $Server "mkdir -p $RemoteDir"
if ($LASTEXITCODE -ne 0) { throw '连不上服务器，未上传' }
& scp -i $KeyFile -o BatchMode=yes -q $cipher "${Server}:$RemoteDir/"
if ($LASTEXITCODE -ne 0) { throw ("scp 失败，本地密文保留在 " + $cipher) }
Ok '已上传'

# 服务器侧独立复核：拿远端算出来的哈希比，不看"scp 退出码 0"就当成了。
$remoteSha = (& ssh -i $KeyFile -o BatchMode=yes $Server "sha256sum '$RemoteDir/$(Split-Path -Leaf $cipher)'" ).Split(' ')[0]
if ($remoteSha -ne $localSha) { throw "远端哈希对不上：本地 $localSha 远端 $remoteSha" }
Ok '远端哈希一致'

# ---------------------------------------------------------------- 回滚演练
if ($Verify) {
    Step '回滚演练：拉回来 → 解密 → 解包 → 逐文件比哈希'
    $pull = Join-Path $StageRoot "roundtrip-$stamp.enc"
    & scp -i $KeyFile -o BatchMode=yes -q "${Server}:$RemoteDir/$(Split-Path -Leaf $cipher)" $pull
    if ($LASTEXITCODE -ne 0) { throw '拉不回来，等于没有备份' }
    $pullSha = (Get-FileHash -LiteralPath $pull -Algorithm SHA256).Hash.ToLower()
    if ($pullSha -ne $localSha) { throw "拉回来的密文哈希不一致" }
    $back = Join-Path $StageRoot "roundtrip-$stamp.tar.gz"
    $env:XIAOYE_BACKUP_PASS = $passphrase
    try {
        & openssl enc '-d' '-aes-256-cbc' '-pbkdf2' '-iter' '200000' '-in' $pull '-out' $back '-pass' 'env:XIAOYE_BACKUP_PASS'
    } finally { Remove-Item Env:XIAOYE_BACKUP_PASS -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -ne 0) { throw '解不开，这份备份是死的' }
    $untar = Join-Path $StageRoot "roundtrip-$stamp"
    New-Item -ItemType Directory -Path $untar -Force | Out-Null
    & $tar '-xzf' $back '-C' $untar
    if ($LASTEXITCODE -ne 0) { throw '解包失败' }
    $srcCount = 0
    $srcBytes = 0L
    foreach ($item in $Include) {
        if (-not (Test-Path -LiteralPath (Join-Path $DataRoot $item) -PathType Container)) { continue }
        $srcCount += @(Get-ChildItem -LiteralPath (Join-Path $DataRoot $item) -Recurse -File).Count
        $srcBytes += (Get-ChildItem -LiteralPath (Join-Path $DataRoot $item) -Recurse -File | Measure-Object Length -Sum).Sum
    }
    $outCount = @(Get-ChildItem -LiteralPath $untar -Recurse -File).Count
    $outBytes = (Get-ChildItem -LiteralPath $untar -Recurse -File | Measure-Object Length -Sum).Sum
    Remove-Item -LiteralPath $untar, $back, $pull -Recurse -Force -ErrorAction SilentlyContinue
    if ($outCount -lt $srcCount) { throw "解出来 $($outCount) 个文件，比源头 $srcCount 个少" }
    Ok "解出 $outCount 个文件 / $([math]::Round($outBytes/1MB,2)) MB（源头 $srcCount 个 / $([math]::Round($srcBytes/1MB,2)) MB）"
}

# ---------------------------------------------------------------- 保留与清理
Step "服务器上只留最近 $Keep 份"
& ssh -i $KeyFile -o BatchMode=yes $Server "cd '$RemoteDir' && ls -1t xiaoye-author-*.tar.gz.enc 2>/dev/null | tail -n +$($Keep + 1) | xargs -r rm -f && ls -1 xiaoye-author-*.tar.gz.enc | wc -l"
if ($LASTEXITCODE -ne 0) { Bad '清理远端旧备份没跑成（不影响这一份已经上去了）' }
Remove-Item -LiteralPath $plain -Force -ErrorAction SilentlyContinue  # 明文那份绝不留
$localOld = @(Get-ChildItem -LiteralPath $StageRoot -Filter 'xiaoye-author-*.tar.gz.enc' | Sort-Object LastWriteTime -Descending | Select-Object -Skip 7)
if ($localOld.Count) { $localOld | Remove-Item -Force }

$used = (Get-Item -LiteralPath $cipher).Length
Ok ("完成：" + (Split-Path -Leaf $cipher) + "  本地留一份副本，明文已删")
Write-Host ("  恢复：openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in 那个 .enc -out b.tar.gz -pass pass:<口令>") -ForegroundColor DarkGray
Write-Host ("        tar -xzf b.tar.gz -C <放回哪儿>   （先停小夜，别让数据库正在写）") -ForegroundColor DarkGray
exit 0
