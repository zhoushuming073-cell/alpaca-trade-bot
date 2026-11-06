$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root '.venv/Scripts/pythonw.exe'
$script = Join-Path $PSScriptRoot 'paper_recovery.py'
$task = 'AlpacaAIPaperRecovery'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $script + '"') -WorkingDirectory $root
$existing = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
if ($existing) {
    Set-ScheduledTask -TaskName $task -Action $action | Out-Null
} else {
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Description 'Recover Paper autopilot after network/power interruption (silent)' | Out-Null
}
$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$vbs = Join-Path $startup 'AlpacaAIRecovery.vbs'
$content = 'Set sh = CreateObject("WScript.Shell")' + "`r`n" + 'sh.Run """' + $pythonw + '"" ""' + $script + '""", 0, False' + "`r`n"
Set-Content -LiteralPath $vbs -Value $content -Encoding ASCII
$old = Join-Path $startup 'AlpacaAIRecovery.cmd'
if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Force }
Get-ScheduledTaskInfo -TaskName $task | Select-Object TaskName,NextRunTime,LastTaskResult
Write-Output ('Startup launcher: ' + $vbs)
