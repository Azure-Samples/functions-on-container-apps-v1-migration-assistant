param(
    [Alias("h")]
    [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($Help) {
    @"
Usage: pwsh -File ./setup.ps1

Check Python 3.10+, create .venv, and install requirements.

Examples:
  pwsh -File ./setup.ps1
  pwsh -File ./setup.ps1 -Help
  pwsh -File ./setup.ps1 -h
"@
    exit 0
}

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $RootDir ".venv"
Set-Location $RootDir
$RunningOnWindows = $env:OS -eq "Windows_NT"

$PythonCommand = $null
$PythonPrefix = @()
$PythonCandidates = if ($RunningOnWindows) {
    @("py", "python", "python3")
} else {
    @("python3", "python", "py")
}
foreach ($candidate in $PythonCandidates) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $CandidatePrefix = if ($candidate -eq "py") { @("-3") } else { @() }
        & $candidate @CandidatePrefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonCommand = $candidate
            $PythonPrefix = $CandidatePrefix
            break
        }
    }
}

if (-not $PythonCommand) {
    throw "Python 3.10 or later is required but was not found."
}

& $PythonCommand @PythonPrefix -c @"
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Python 3.10 or later is required; found {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]}: ready")
"@
if ($LASTEXITCODE -ne 0) {
    throw "Python version validation failed."
}

if (-not (Test-Path $VenvDir)) {
    & $PythonCommand @PythonPrefix -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create virtual environment at $VenvDir."
    }
}

$VenvPython = if ($RunningOnWindows) {
    Join-Path $VenvDir "Scripts/python.exe"
} else {
    Join-Path $VenvDir "bin/python"
}
if (-not (Test-Path $VenvPython)) {
    throw "The existing .venv is not compatible with this platform. Remove .venv and rerun setup.ps1."
}
& $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "The existing .venv uses Python older than 3.10. Remove .venv and rerun setup.ps1."
}

Write-Host "Installing dependencies..."
& $VenvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip."
}

& $VenvPython -m pip install --quiet -r (Join-Path $RootDir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install requirements.txt."
}

$ActivatePath = if ($RunningOnWindows) {
    Join-Path $VenvDir "Scripts/Activate.ps1"
} else {
    Join-Path $VenvDir "bin/Activate.ps1"
}
Write-Host ""
Write-Host "Setup complete."
Write-Host "Activate with: & `"$ActivatePath`""
