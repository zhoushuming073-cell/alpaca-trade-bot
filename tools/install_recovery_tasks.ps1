$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$script = Join-Path $PSScriptRoot 'start_recovery.ps1'
$action = '"' + $ps + '" -NoProfile -ExecutionPolicy Bypass -File "' + $script + '"'
$minute = & schtasks.exe /Create /TN 'AlpacaAIPaperRecovery' /SC MINUTE /MO 2 /TR $action /F 2>&1
Write-Output $minute
$logon = & schtasks.exe /Create /TN 'AlpacaAIPaperRecoveryLogon' /SC ONLOGON /TR $action /F 2>&1
Write-Output $logon
