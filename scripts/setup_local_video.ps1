# Prepare local video components in an existing ComfyUI on a non-system drive.
[CmdletBinding()]
param(
  [string]$EngineRoot = 'D:\AI\XiaoYe\engine\ComfyUI',
  [string]$Python = 'D:\AI\XiaoYe\engine\venv\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
$resolved = [IO.Path]::GetFullPath($EngineRoot)
if ([IO.Path]::GetPathRoot($resolved) -eq [IO.Path]::GetPathRoot($env:SystemRoot)) {
  throw '本地视频引擎与模型必须放在非系统盘。'
}
if (-not (Test-Path -LiteralPath (Join-Path $resolved 'main.py'))) {
  throw '请先准备 ComfyUI，然后通过 -EngineRoot 指定目录。'
}
if (-not (Test-Path -LiteralPath $Python)) { throw '找不到 ComfyUI 的 Python 环境。' }
$cache = Join-Path (Split-Path -Parent $resolved) 'cache'
$temporary = Join-Path (Split-Path -Parent $resolved) 'tmp'
New-Item -ItemType Directory -Force -Path $temporary,$cache | Out-Null
$env:TEMP=$temporary; $env:TMP=$temporary
$env:PIP_CACHE_DIR=Join-Path $cache 'pip'
$env:HF_HOME=Join-Path $cache 'huggingface'
$env:TORCH_HOME=Join-Path $cache 'torch'
foreach ($component in @(
  @('ComfyUI-AnimateDiff-Evolved','https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved.git'),
  @('ComfyUI-VideoHelperSuite','https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git')
)) {
  $target=Join-Path (Join-Path $resolved 'custom_nodes') $component[0]
  if (-not (Test-Path -LiteralPath $target)) {
    & git clone --depth 1 $component[1] $target
    if ($LASTEXITCODE -ne 0) { throw '视频组件下载失败。' }
  }
}
& $Python -m pip install -r (Join-Path $resolved 'custom_nodes\ComfyUI-VideoHelperSuite\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '视频依赖安装失败。' }
foreach ($model in @(
  @{ Name='animatediff_models\mm_sd15_v3.safetensors'; Url='https://huggingface.co/conrevo/AnimateDiff-A1111/resolve/main/motion_module/mm_sd15_v3.safetensors'; Hash='a8ca0eadb18c6ad6652bca344a458f31780901a87f765bf95a81ef9c5be55f29' }
)) {
  $target=Join-Path (Join-Path $resolved 'models') $model.Name
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
  $valid = (Test-Path -LiteralPath $target) -and ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq $model.Hash)
  if (-not $valid) {
    $part=$target+'.partial'
    & curl.exe -L --fail --retry 3 --output $part $model.Url
    if ($LASTEXITCODE -ne 0) { throw '模型下载失败，保留 partial 文件供诊断。' }
    if ((Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash -ne $model.Hash) { throw '模型 SHA256 不匹配，不启用此文件。' }
    Move-Item -LiteralPath $part -Destination $target -Force
  }
}
Write-Output "视频组件已准备好：$resolved。重启 ComfyUI，然后在软件生图设置中指定此目录。"
