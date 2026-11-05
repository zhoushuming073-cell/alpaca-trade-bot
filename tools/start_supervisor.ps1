$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv/Scripts/python.exe'
$script = Join-Path $PSScriptRoot 'paper_supervisor.py'
$out = Join-Path $root 'logs/supervisor-stdout.log'
$err = Join-Path $root 'logs/supervisor-stderr.log'
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'paper_supervisor\.py' }
if ($existing) { return }
if (-not (Test-Path -LiteralPath $python)) { throw 'Python venv missing' }
Start-Process -FilePath $python -ArgumentList $script -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null
