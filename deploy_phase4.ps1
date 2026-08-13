param(
    [string]$TargetRoot = "E:\AI\Iris",
    [string]$DataRoot = "E:\AI\Assistant\data",
    [string]$PythonExe = "py -3.11",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$sourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $sourceRoot) {
    $sourceRoot = (Get-Location).Path
}

$appRoot = $TargetRoot
$runtimeRoot = Join-Path $TargetRoot "Runtime"
$venvRoot = Join-Path $runtimeRoot "venv"
$dataRoot = $DataRoot

Write-Host "Deploying from: $sourceRoot"
Write-Host "Deploying to:   $TargetRoot"
Write-Host "Using data at:  $dataRoot"

New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null

foreach ($name in @("memory", "sessions", "proposals", "audit", "index", "documents")) {
    New-Item -ItemType Directory -Path (Join-Path $dataRoot $name) -Force | Out-Null
}

$memoryDir = Join-Path $dataRoot "memory"
$sourceMemoryDir = Join-Path $sourceRoot "data\memory"
$seedFiles = @{
    "profile.json" = "{`n  `"schema_version`": 1,`n  `"profile`": {},`n  `"metadata`": {}`n}`n"
    "preferences.json" = "{`n  `"schema_version`": 1,`n  `"preferences`": [],`n  `"metadata`": {}`n}`n"
    "projects.json" = "{`n  `"schema_version`": 1,`n  `"projects`": [],`n  `"metadata`": {}`n}`n"
    "knowledge.json" = "{`n  `"schema_version`": 1,`n  `"knowledge_areas`": [],`n  `"documents`": [],`n  `"metadata`": {}`n}`n"
}
foreach ($fileName in $seedFiles.Keys) {
    $seedPath = Join-Path $memoryDir $fileName
    if (-not (Test-Path $seedPath)) {
        $sourceSeed = Join-Path $sourceMemoryDir $fileName
        if (Test-Path $sourceSeed) {
            Copy-Item -LiteralPath $sourceSeed -Destination $seedPath -Force
        }
        else {
            Set-Content -LiteralPath $seedPath -Value $seedFiles[$fileName] -Encoding UTF8
        }
    }
}

$excludeDirs = @(
    ".git",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    "Data",
    "data",
    ".pack-staging"
)

$excludeFiles = @(
    "*.pyc",
    "*.pyo",
    "release.zip"
)

$robocopyArgs = @(
    $sourceRoot,
    $appRoot,
    "/E",
    "/R:1",
    "/W:1",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS",
    "/NP"
)

foreach ($dir in $excludeDirs) {
    $robocopyArgs += @("/XD", $dir)
}
foreach ($file in $excludeFiles) {
    $robocopyArgs += @("/XF", $file)
}

& robocopy @robocopyArgs | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path $venvRoot)) {
    Write-Host "Creating virtual environment..."
    Invoke-Expression "$PythonExe -m venv `"$venvRoot`""
}

$venvPython = Join-Path $venvRoot "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found at $venvPython"
}

Write-Host "Installing dependencies..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $appRoot "core\requirements.txt")

$configPath = Join-Path $appRoot "core\config.json"
if (-not (Test-Path $configPath)) {
    throw "Config file not found: $configPath"
}

$config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$config.memory_path = (Join-Path $dataRoot "memory")
$config.session_path = (Join-Path $dataRoot "sessions")
$config.proposal_path = (Join-Path $dataRoot "proposals")
$config.audit_path = (Join-Path $dataRoot "audit")
$config.action_audit_path = (Join-Path $dataRoot "audit")
$config.document_search.catalog_path = (Join-Path $dataRoot "index\documents.db")
$config.document_search.roots = @((Join-Path $dataRoot "documents"))

$configJson = $config | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($configPath, $configJson + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))

$launcherPath = Join-Path $TargetRoot "Launch-Iris.cmd"
$launcher = @(
    "@echo off",
    "cd /d $appRoot",
    "`"$venvPython`" main.py",
    "if errorlevel 1 (",
    "  echo.",
    "  echo Iris exited with an error. Press any key to close this window.",
    "  pause >nul",
    ")"
)
Set-Content -LiteralPath $launcherPath -Value $launcher -Encoding ASCII

if (-not $SkipTests) {
    Write-Host "Running test suite in deployed app..."
    Push-Location $appRoot
    try {
        & $venvPython "core\run_tests.py"
        if ($LASTEXITCODE -ne 0) {
            throw "Deployed test run failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host "Deployment complete."
Write-Host "Next steps:"
Write-Host "1) Edit $configPath to add real document roots and application executable paths."
Write-Host "2) Ensure Ollama is running: Invoke-RestMethod http://localhost:11434/api/tags"
Write-Host "3) Start Iris with $launcherPath"
