$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$url = 'http://127.0.0.1:4200'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python environment missing. See README.md.' }
$available = $false
try { $response = Invoke-RestMethod "$url/api/status" -TimeoutSec 2; $available = $response.service -eq 'ai-paper-trader' } catch {}
if (-not $available) {
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'logs') | Out-Null
    # Dashboard only.  The worker is never started automatically; the user must
    # click "启动机器人" and then explicitly "恢复交易" after the safety gates.
    Start-Process -FilePath $pythonPath -ArgumentList '-m','trading_bot.dashboard' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'logs/dashboard-out.log') -RedirectStandardError (Join-Path $PSScriptRoot 'logs/dashboard-error.log') | Out-Null
    for ($attempt=0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        try { $response=Invoke-RestMethod "$url/api/status" -TimeoutSec 2; if ($response.service -eq 'ai-paper-trader') {$available=$true;break} } catch {}
    }
}
if (-not $available) { throw 'Dashboard failed to start. Check logs/dashboard-error.log.' }
Start-Process $url
