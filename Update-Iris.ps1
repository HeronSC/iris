param(
    [string]$SourceRoot = "E:\AI\iris",
    [string]$TargetRoot = "E:\AI\Iris",
    [string]$DataRoot = "E:\AI\Assistant\data",
    [string]$Branch = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

Write-Host "Updating Iris from $SourceRoot"
Set-Location $SourceRoot

$status = git status --porcelain
if ($status) {
    throw "The source checkout has uncommitted changes; commit or stash them before updating."
}

$before = git rev-parse --short HEAD
if ($Branch) {
    git checkout $Branch
}
git pull --ff-only
$after = git rev-parse --short HEAD
Write-Host "Source moved $before -> $after"

$backupScript = Join-Path $SourceRoot "core\storage\backups.py"
Write-Host "Backing up Data before the deploy"
$venvPython = Join-Path $SourceRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    & $venvPython -c "import sys; sys.path.insert(0, r'$SourceRoot'); from core.host.knowledge import IrisKnowledgeService; from core.host.service import IrisHost; host = IrisHost(serve_http=False); report = host.backups.run(); print(report.summary)"
}

$deploy = Join-Path $SourceRoot "deploy_phase4.ps1"
$deployArgs = @("-TargetRoot", $TargetRoot, "-DataRoot", $DataRoot)
if ($SkipTests) { $deployArgs += "-SkipTests" }
& $deploy @deployArgs

Write-Host "Data and config were not touched: the deploy copies code and rebuilds the venv only."
Write-Host "Restart the Iris service and window to pick up $after."
