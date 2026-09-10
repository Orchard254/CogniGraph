# One-time setup for project-memory: venv + dependencies + test suite.
# Usage: .\setup.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"

# 0. Require Python 3.12+
$pyVersion = & python --version 2>&1
if ($LASTEXITCODE -ne 0) { throw "Python not found on PATH. Install Python 3.12+ first." }
if ($pyVersion -notmatch 'Python 3\.(\d+)') { throw "Could not parse Python version from: $pyVersion" }
if ([int]$Matches[1] -lt 12) { throw "Python 3.12+ required, found: $pyVersion" }
Write-Host "[setup] Found $pyVersion"

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