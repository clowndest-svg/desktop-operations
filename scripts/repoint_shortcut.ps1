# 把桌面和开始菜单那两枚快捷方式指到某一轮，然后**读回**（读回才算改过）。
#
# 用法： powershell -ExecutionPolicy Bypass -File scripts\repoint_shortcut.ps1 -Round r85
#
# 为什么写这个：历轮脚本往「桌面\小夜.lnk」写过一枚，而盘上真正存在、也真是用户双击的那枚
# 叫「小夜.exe - 快捷方式.lnk」—— 改完读回才发现改的是另一个文件（r84 抓到）。
# 所以这里按**盘上的实情**改：桌面那枚用真实文件名，开始菜单那枚在 小夜\ 子目录里。
param(
  [Parameter(Mandatory = $true)][ValidatePattern("^r[0-9]+$")][string]$Round,
  [string]$Root = 'E:\DaiMa\XiaoYeBuild'
)
$ErrorActionPreference = 'Stop'
$name = [string][char]0x5C0F + [char]0x591C
$appDir = Join-Path (Join-Path $Root $Round) ('dist\' + $name)
$exe = Join-Path $appDir ($name + '.exe')
if (-not (Test-Path -LiteralPath $exe)) { throw ('这一轮没有 exe：' + $exe) }

$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$ks = [char]0x5FEB + [char]0x6377 + [char]0x65B9 + [char]0x5F0F   # 「快捷方式」
$paths = @(
  (Join-Path $desktop ($name + '.exe - ' + $ks + '.lnk')),
  (Join-Path $programs ($name + '\' + $name + '.lnk'))
)

$shell = New-Object -ComObject WScript.Shell
foreach ($path in $paths) {
  $folder = Split-Path -Parent $path
  if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
  $had = Test-Path -LiteralPath $path
  $link = $shell.CreateShortcut($path)
  $link.TargetPath = $exe
  $link.WorkingDirectory = $appDir
  $link.IconLocation = ($exe + ',0')
  $link.Description = ('小夜 桌面助手 · ' + $Round)
  $link.Save()
  Write-Output ('写了 ' + $path + '（本来存在: ' + $had + '）')
}

Write-Output '--- 读回 ---'
$bad = 0
foreach ($path in $paths) {
  if (-not (Test-Path $path)) { Write-Output ('缺失 ' + $path); $bad++; continue }
  $back = $shell.CreateShortcut($path)
  if ($back.TargetPath -eq $exe -and $back.WorkingDirectory -eq $appDir -and $back.IconLocation -eq ($exe + ",0")) { $mark = 'OK  ' } else { $mark = '错  '; $bad++ }
  Write-Output ($mark + $path + ' => ' + $back.TargetPath)
}
if ($bad -gt 0) { exit 1 }

exit 0
