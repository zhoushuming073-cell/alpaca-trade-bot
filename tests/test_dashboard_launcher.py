"""Run the Windows launcher with fake HTTP and process creation; no services start."""
import shutil
import subprocess
from pathlib import Path

import pytest

SHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(not SHELL, reason="Windows PowerShell launcher")


@pytest.mark.parametrize("mode,expected", [("reuse", "0/1"), ("cold", "1/1"), ("failure", "1/0")])
def test_launcher_waits_before_browser_and_never_starts_worker(tmp_path, mode, expected):
    source = Path(__file__).resolve().parents[1] / "start-dashboard.ps1"
    shutil.copy(source, tmp_path / source.name)
    python = tmp_path / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (tmp_path / ".venv/Scripts/pythonw.exe").touch()
    harness = tmp_path / "test.ps1"
    harness.write_text(r'''
$ErrorActionPreference = 'Stop'
$global:launches = 0
$global:browsers = 0
$global:probes = 0
function Invoke-RestMethod {
    param($Uri, $TimeoutSec)
    $global:probes++
    if ($env:LAUNCH_TEST_MODE -eq 'reuse' -or $global:probes -ge 3) {
        return @{service='ai-paper-trader';ready=$true}
    }
    throw 'Not ready'
}
function Start-Sleep { param($Milliseconds) }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError)
    if ($FilePath -like 'http*') {
        if ($env:LAUNCH_TEST_MODE -ne 'reuse' -and $global:probes -lt 3) { throw 'Browser opened too early' }
        $global:browsers++
    } else {
        $global:launches++
        if (($ArgumentList -join ' ') -ne '-m trading_bot.dashboard') { throw 'Unexpected process' }
        if ($WindowStyle -ne 'Hidden') { throw 'Service must be hidden' }
        if ($env:LAUNCH_TEST_MODE -eq 'failure') { throw 'Injected startup failure' }
    }
}
try { & "$PSScriptRoot/start-dashboard.ps1" }
catch { if ($env:LAUNCH_TEST_MODE -ne 'failure') { throw } }
Write-Output "RESULT:$global:launches/$global:browsers"
''', encoding="ascii")
    import os
    result = subprocess.run([SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                            env={**os.environ, "LAUNCH_TEST_MODE": mode}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert f"RESULT:{expected}" in result.stdout, result.stdout
