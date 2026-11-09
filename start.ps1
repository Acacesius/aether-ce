# Start AETHER CE as a plain Python process on 127.0.0.1 (no venv, no pip). Needs Python 3.11+.
# If scripts are blocked, run once:  powershell -ExecutionPolicy Bypass -File .\start.ps1
# (or: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned). start.bat works without PowerShell.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$check = 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)'
$found = $null
foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
    if (-not (Get-Command $cand[0] -ErrorAction SilentlyContinue)) { continue }
    $extra = @($cand | Select-Object -Skip 1)
    & $cand[0] @extra -c $check *> $null
    if ($LASTEXITCODE -eq 0) { $found = $cand; break }
}
if (-not $found) {
    Write-Host 'Python 3.11 or newer is required but was not found.'
    Write-Host 'Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".'
    exit 1
}
$env:PYTHONPATH = $PSScriptRoot
$extra = @($found | Select-Object -Skip 1)
& $found[0] @extra -m aether.studio serve --open @args
exit $LASTEXITCODE
