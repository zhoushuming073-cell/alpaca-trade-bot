$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root '.venv/Scripts/pythonw.exe'
$script = Join-Path $PSScriptRoot 'paper_recovery.py'
$existing = Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -match 'paper_recovery\.py' }
if ($existing) { return }
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Python venv missing' }
Start-Process -FilePath $pythonw -ArgumentList $script -WorkingDirectory $root -WindowStyle Hidden | Out-Null
