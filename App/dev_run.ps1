param(
    [ValidateSet("console", "ui", "tests")]
    [string]$Mode = "ui",
    [string]$ConfigPath = "",
    [switch]$Bootstrap,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $appRoot) {
    $appRoot = (Get-Location).Path
}

Set-Location $appRoot

$venvPython = Join-Path $appRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    if (-not $Bootstrap) {
        throw "Missing local venv at $venvPython. Re-run with -Bootstrap to create it."
    }

    Write-Host "Creating local .venv..."
    py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create .venv with py -3.11"
    }

    Write-Host "Installing base dependencies..."
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r (Join-Path $appRoot "core\requirements.txt")
    & $venvPython -m pip install -r (Join-Path $appRoot "ui\requirements.txt")
}

if ($ConfigPath) {
    $resolvedConfig = (Resolve-Path -Path $ConfigPath).Path
    $env:IRIS_CONFIG_PATH = $resolvedConfig
}

$args = @()
if ($Mode -eq "console") {
    $args = @("core\main.py")
}
elseif ($Mode -eq "ui") {
    $args = @("ui\main.py")
}
else {
    $env:QT_QPA_PLATFORM = "offscreen"
    $args = @(
        "-m", "pytest",
        "core\test_phase3.py",
        "core\test_phase4.py",
        "core\test_phase4_8.py",
        "core\test_phase4_8_slice2.py",
        "core\tests\test_ui_phase1_behavior.py"
    )
}

if ($DryRun) {
    $cmd = "$venvPython " + ($args -join " ")
    Write-Host "Dry run command: $cmd"
    if ($ConfigPath) {
        Write-Host "IRIS_CONFIG_PATH=$env:IRIS_CONFIG_PATH"
    }
    exit 0
}

& $venvPython @args
exit $LASTEXITCODE
