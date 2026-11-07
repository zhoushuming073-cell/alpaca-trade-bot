$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/pythonw.exe'
$url = 'http://127.0.0.1:4200'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python environment missing. See README.md.' }
$launchMutex = New-Object System.Threading.Mutex($false, 'Local\AlpacaDashboardLauncher4200')
$ownsMutex = $false
try {
try { $ownsMutex = $launchMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
if (-not $ownsMutex) { Write-Host 'Another launcher is already starting Dashboard. Please wait.'; return }
$available = $false
Write-Host 'Checking Dashboard at http://127.0.0.1:4200 ...'
try { $response = Invoke-RestMethod "$url/api/health" -TimeoutSec 2; $available = $response.service -eq 'ai-paper-trader' -and $response.ready } catch {}
if (-not $available) {
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'logs') | Out-Null
    # Dashboard only. Never change worker or trading controls here.
    Write-Host 'Loading Dashboard. First startup may take up to two minutes...'
    Start-Process -FilePath $pythonPath -ArgumentList '-m','trading_bot.dashboard' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'logs/dashboard-out.log') -RedirectStandardError (Join-Path $PSScriptRoot 'logs/dashboard-error.log') | Out-Null
    $startupTimer = [Diagnostics.Stopwatch]::StartNew()
    while ($startupTimer.Elapsed.TotalSeconds -lt 120) {
        Write-Progress -Activity 'Starting Dashboard' -Status ('Waiting for local service: {0}s' -f [int]$startupTimer.Elapsed.TotalSeconds)
        Start-Sleep -Milliseconds 500
        try { $response=Invoke-RestMethod "$url/api/health" -TimeoutSec 2; if ($response.service -eq 'ai-paper-trader' -and $response.ready) {$available=$true;break} } catch {}
    }
    Write-Progress -Activity 'Starting Dashboard' -Completed
}
if (-not $available) { throw 'Dashboard not ready after 120 seconds. Check logs/dashboard-error.log. No worker was stopped; do not click trading controls to fix a web connection.' }
Write-Host 'Dashboard ready. Opening browser. Trading state is unchanged.'
Start-Process $url
} finally {
    if ($ownsMutex) { $launchMutex.ReleaseMutex() }
    $launchMutex.Dispose()
}
