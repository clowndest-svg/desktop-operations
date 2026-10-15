# Fetch the official fixed x64 runtime once on the AUTHOR machine. Buyers stay offline.
[CmdletBinding()]
param([string]$Root='D:\AI\XiaoYe\resources\webview2')
$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Path $Root -Force | Out-Null
$manifest=Join-Path $Root 'runtime.json'
if (Test-Path -LiteralPath $manifest) {
    $saved=Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if (Test-Path -LiteralPath (Join-Path $saved.path 'msedgewebview2.exe')) { return }
}
$html=(Invoke-WebRequest -UseBasicParsing -Uri 'https://developer.microsoft.com/en-us/microsoft-edge/webview2/').Content
$decoded=$html.Replace('\u002F','/')
$match=[regex]::Match($decoded,'https://msedge\.sf\.dl\.delivery\.mp\.microsoft\.com/[^"\s<>]+\.x64\.cab')
if (-not $match.Success) { throw 'Official fixed WebView2 x64 download URL not found' }
$url=$match.Value
$cab=Join-Path $Root ([IO.Path]::GetFileName($url))
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $cab
$expanded=Join-Path $Root 'expanded'
New-Item -ItemType Directory -Path $expanded -Force | Out-Null
& expand.exe $cab '-F:*' $expanded | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'WebView2 CAB extraction failed' }
$exe=Get-ChildItem -LiteralPath $expanded -Filter msedgewebview2.exe -File -Recurse | Select-Object -First 1
if (-not $exe) { throw 'WebView2 executable missing after extraction' }
$sig=Get-AuthenticodeSignature -LiteralPath $exe.FullName
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike '*Microsoft Corporation*') {
    throw 'WebView2 executable did not pass Microsoft Authenticode validation'
}
@{path=$exe.DirectoryName;url=$url;cab_sha256=(Get-FileHash -LiteralPath $cab -Algorithm SHA256).Hash;version=$exe.VersionInfo.FileVersion} |
    ConvertTo-Json | Set-Content -LiteralPath $manifest -Encoding UTF8
Write-Output "Official fixed WebView2 ready: $($exe.DirectoryName)"
