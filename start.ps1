$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the virtual environment. Install Python 3.10 or newer.' }
}
& $taskPython -c "import importlib.util, sys; sys.exit(not all(importlib.util.find_spec(m) for m in ('fastapi', 'uvicorn', 'selenium', 'requests', 'PIL')))"
if ($LASTEXITCODE -ne 0) {
    & $taskPython -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network and retry.' }
}
& $taskPython run_console.py @args
