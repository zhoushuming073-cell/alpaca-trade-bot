$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$task = 'AlpacaAIPaperSupervisor'
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$script = Join-Path $PSScriptRoot 'start_supervisor.ps1'
$action = '"' + $ps + '" -NoProfile -ExecutionPolicy Bypass -File "' + $script + '"'
$result = & schtasks.exe /Create /TN $task /SC DAILY /ST 20:30 /TR $action /F 2>&1
Write-Output $result
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& schtasks.exe /Query /TN $task /FO LIST /V 2>&1 | Select-String -Pattern 'TaskName|Status|Next Run Time|Schedule Type|Start Time|Task To Run'
