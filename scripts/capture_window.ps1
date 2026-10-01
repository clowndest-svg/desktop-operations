# Capture a window's client area to a PNG, by process id.
#
# Why PrintWindow rather than CopyFromScreen: the HUD is a WebView2 control, and
# its content is composited by DirectComposition. A screen-region grab only works
# while the window is unobscured and on top, which makes it useless for an
# automated check — and a check that silently captures the desktop behind a
# minimised window is worse than no check. PW_RENDERFULLCONTENT (0x2) is what
# asks the window to render itself, including composited children.
#
# The DWM frame is trimmed: a window's GetWindowRect includes the invisible
# resize border (7px left/right, and a taller strip at the bottom on Windows 11),
# so a naive crop keeps a band of whatever is behind the window.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\capture_window.ps1 -TargetPid 1234 -Out shot.png

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$TargetPid,
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$InsetX = 7,
    [int]$InsetTop = 0,
    [int]$InsetBottom = 7
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing

if (-not ("WinCap" -as [type])) {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinCap {
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hWnd, int attr, out RECT value, int size);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
}

$process = Get-Process -Id $TargetPid -ErrorAction Stop
$handle = $process.MainWindowHandle
if ($handle -eq [IntPtr]::Zero) {
    throw "进程 $TargetPid 没有主窗口句柄（窗口可能还没建好）"
}

# Ask DWM for the *visible* frame; fall back to GetWindowRect when it is unavailable.
$rect = New-Object WinCap+RECT
$dwm = [WinCap]::DwmGetWindowAttribute($handle, 9, [ref]$rect, 16)  # DWMWA_EXTENDED_FRAME_BOUNDS
if ($dwm -ne 0) {
    [void][WinCap]::GetWindowRect($handle, [ref]$rect)
}

# Bring it forward so the compositor has actually drawn it at least once.
[void][WinCap]::ShowWindow($handle, 5)   # SW_SHOW
[void][WinCap]::SetForegroundWindow($handle)
Start-Sleep -Milliseconds 900

$left = $rect.Left + $InsetX
$top = $rect.Top + $InsetTop
$width = ($rect.Right - $rect.Left) - (2 * $InsetX)
$height = ($rect.Bottom - $rect.Top) - $InsetTop - $InsetBottom
if ($width -le 0 -or $height -le 0) {
    throw "窗口尺寸无效：${width}x${height}"
}

$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$hdc = $graphics.GetHdc()
try {
    # 0x2 = PW_RENDERFULLCONTENT
    $ok = [WinCap]::PrintWindow($handle, $hdc, 2)
}
finally {
    $graphics.ReleaseHdc($hdc)
    $graphics.Dispose()
}

if (-not $ok) {
    $bitmap.Dispose()
    throw "PrintWindow 失败"
}

$directory = Split-Path -Parent $Out
if ($directory -and -not (Test-Path -LiteralPath $directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$bitmap.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bitmap.Dispose()

Write-Output ("已保存 {0}（{1}x{2}）" -f $Out, $width, $height)
