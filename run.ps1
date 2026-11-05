param(
    [string]$Command = 'check',
    [string]$Mode = 'paper',
    [string]$Cid = '',
    [string]$Reason = '',
    [switch]$Confirm
)
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$legacyKeyPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'api-key.txt'
# Reuse legacy local secret only in this process environment; never echo or copy.
if (-not $env:DEEPSEEK_API_KEY -and (Test-Path -LiteralPath $legacyKeyPath)) {
    $env:DEEPSEEK_API_KEY = (Get-Content -LiteralPath $legacyKeyPath -Raw).Trim()
}
$alpacaKeyPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'alpaca key.txt'
if (Test-Path -LiteralPath $alpacaKeyPath) {
    $credentialText = Get-Content -LiteralPath $alpacaKeyPath -Raw
    if (-not $env:ALPACA_API_KEY -and $credentialText -match '(?im)^Key[\s:：=]+(\S+)') {
        $env:ALPACA_API_KEY = $Matches[1]
    }
    if (-not $env:ALPACA_API_SECRET -and $credentialText -match '(?im)^Secret[\s:：=]+(\S+)') {
        $env:ALPACA_API_SECRET = $Matches[1]
    }
    $credentialText = $null
}
$arguments = @('-m','trading_bot.main',$Command,'--mode',$Mode)
if ($Cid) { $arguments += @('--cid', $Cid) }
if ($Reason) { $arguments += @('--reason', $Reason) }
if ($Confirm) { $arguments += '--confirm' }
& $pythonPath @arguments
exit $LASTEXITCODE
