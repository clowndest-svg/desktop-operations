# Per-process CPU and memory, measured over a wall-clock interval.
#
# Why not `Get-Process | Select CPU`: that `CPU` value is a *lifetime* total, so one
# sample tells you what a process has ever done, not what it is doing now. And why
# not `Get-Counter "\Process(name)\% Processor Time"`: instance names collide the
# moment several processes share a name -- which this app does on purpose, since
# WebView2 runs a browser, a renderer, a GPU process and utilities all under
# msedgewebview2 -- and a collated counter quietly answers with the first match.
#
# So: difference the raw processor-time counter across the process tree, divide by
# the elapsed wall time, and report cores. "0.22%" asks a question; "0.002 cores"
# answers one.
#
# The counter's naming is the trap that ate the first version of this script. The
# perf classes key on `IDProcess`, not `Id` or `ProcessId`, and the raw class has no
# `TotalProcessorTime` at all -- the cumulative 100 ns processor-time counter is
# called `PercentProcessorTime` there, and only the *formatted* class turns it into
# a percentage.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\measure_process.ps1 -TargetPid 1234 -Seconds 20

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$TargetPid,
    [int]$Seconds = 15,
    [switch]$IncludeChildren
)

$ErrorActionPreference = 'Stop'

function Get-Tree([int]$Id) {
    $ids = @($Id)
    if ($IncludeChildren) {
        # Win32_Process is the only place the parent link exists; the perf classes
        # carry no ParentProcessId.
        $ids += @(
            Get-CimInstance -Query "SELECT ProcessId FROM Win32_Process WHERE ParentProcessId=$Id" |
                ForEach-Object { $_.ProcessId }
        )
    }
    return @($ids | Where-Object { $_ -ne $null } | Select-Object -Unique)
}

function Get-Sample([int[]]$Ids) {
    $cpu = [double]0
    $mem = [double]0
    $found = 0
    foreach ($id in $Ids) {
        # 0 is the Idle pseudo-instance; counting it would bill the whole machine's
        # doing-nothing to the process under test.
        if ($id -eq 0) { continue }
        $raw = @(Get-CimInstance -Query "SELECT PercentProcessorTime FROM Win32_PerfRawData_PerfProc_Process WHERE IDProcess=$id")
        if ($raw.Count -eq 0) { continue }
        $found += 1
        foreach ($entry in $raw) { $cpu += [double]$entry.PercentProcessorTime }
        $form = @(Get-CimInstance -Query "SELECT WorkingSet FROM Win32_PerfFormattedData_PerfProc_Process WHERE IDProcess=$id")
        foreach ($entry in $form) { $mem += [double]$entry.WorkingSet }
    }
    return [pscustomobject]@{ Cpu100ns = $cpu; Bytes = $mem; Found = $found }
}

$ids = Get-Tree $TargetPid
$clock = [System.Diagnostics.Stopwatch]::StartNew()
$before = Get-Sample $ids
if ($before.Found -eq 0) {
    Write-Error "no perf counters for pid $TargetPid (is it still running?)"
    exit 2
}
Start-Sleep -Seconds $Seconds
$after = Get-Sample $ids
$clock.Stop()

$elapsed100ns = $clock.Elapsed.TotalSeconds * 1e7
$cores = ($after.Cpu100ns - $before.Cpu100ns) / $elapsed100ns

Write-Output ("target      : pid {0}{1}, {2} process(es) counted" -f $TargetPid, $(if ($IncludeChildren) { " + children" } else { "" }), $after.Found)
Write-Output ("window      : {0:N1}s" -f $clock.Elapsed.TotalSeconds)
Write-Output ("cpu         : {0:N3} cores ({1:N2}% of one core)" -f $cores, ($cores * 100))
Write-Output ("working set : {0:N0} MB" -f ($after.Bytes / 1MB))
