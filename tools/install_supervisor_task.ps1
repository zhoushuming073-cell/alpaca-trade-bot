$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root '.venv/Scripts/pythonw.exe'
$script = Join-Path $PSScriptRoot 'paper_supervisor.py'
$task = 'AlpacaAIPaperSupervisor'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $script + '"') -WorkingDirectory $root
$existing = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
if ($existing) {
    Set-ScheduledTask -TaskName $task -Action $action | Out-Null
} else {
    $trigger = New-ScheduledTaskTrigger -Daily -At '20:30'
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Description 'Start the Paper trading supervisor daily (silent)' | Out-Null
}
Get-ScheduledTaskInfo -TaskName $task | Select-Object TaskName,NextRunTime,LastTaskResult
