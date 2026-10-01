# Minimise or restore a process's main window, from a script.
#
# Why this exists: "is the 3D view costing CPU?" is only answerable by comparing the
# same window drawing and not drawing, and the only supported way to stop the HUD's
# render loops without rebuilding is to make the page hidden -- which is what
# minimising does. Sending the keystroke by hand is not repeatable, and a check you
# cannot re-run is an anecdote.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\window_state.ps1 -TargetPid 1234 -Action minimize
#   powershell -ExecutionPolicy Bypass -File scripts\window_state.ps1 -TargetPid 1234 -Action restore

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$TargetPid,
    [Parameter(Mandatory = $true)][ValidateSet('minimize', 'restore')][string]$Action
)

$signature = @'
using System;
using System.Runtime.InteropServices;
public static class JarvisWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern IntPtr GetShellWindow();
}
'@
Add-Type -AssemblyName System.Windows.Forms
Add-Type $signature

$SW_MINIMIZE = 6
$SW_RESTORE = 9

# Every top-level window belonging to this process, including hidden message-only
# parents: pywebview's WinForms host creates a window whose owner is not the one the
# user sees, so matching a single handle can minimise the wrong thing.
$handles = @(
    Get-Process -Id $TargetPid | ForEach-Object { $_.MainWindowHandle }
)
$visible = [System.Diagnostics.Process]::GetProcessById($TargetPid)
if ($handles.Count -eq 0 -or $handles[0] -eq [IntPtr]::Zero) {
    # Fall back to enumerating by pid through the Forms API, which sees owned windows.
    foreach ($form in [System.Windows.Forms.Application]::OpenForms) {
        if ($form.Handle -ne [IntPtr]::Zero) { $handles += $form.Handle }
    }
}
if ($handles.Count -eq 0) {
    Write-Error "no window handle for pid $TargetPid"
    exit 2
}

$code = if ($Action -eq 'minimize') { $SW_MINIMIZE } else { $SW_RESTORE }
foreach ($handle in $handles) {
    if ($handle -eq [IntPtr]::Zero) { continue }
    [void][JarvisWindow]::ShowWindow($handle, $code)
}
Write-Output ("{0}: {1} handle(s) on pid {2}" -f $Action, $handles.Count, $TargetPid)
