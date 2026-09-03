# One-time setup for project-memory: venv + dependencies + test suite.
# Usage: .\setup.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"

# 1. Create the virtual environment if missing
if (-not (Test-Path $venvPython)) {
    Write-Host "[setup] Creating Python virtual environment in venv/ ..."
    python -m venv venv
}

# 2. Install / refresh dependencies
Write-Host "[setup] Installing dependencies from requirements.txt ..."
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt

# 3. Run the test suite
Write-Host "[setup] Running test suite ..."
& $venvPython -m pytest -q

Write-Host ""
Write-Host "[setup] Done. Next steps:"
Write-Host "  .\venv\Scripts\python.exe memory_cli.py wire cursor --root C:\path\to\your\project"
Write-Host "  .\venv\Scripts\python.exe memory_cli.py bootstrap --root C:\path\to\your\project --apply"

</output>