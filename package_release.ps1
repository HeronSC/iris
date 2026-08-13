param(
    [string]$OutputPath = "release.zip"
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $root) {
    $root = Get-Location
}

$outputFullPath = Join-Path $root $OutputPath
if (Test-Path -LiteralPath $outputFullPath) {
    Remove-Item -LiteralPath $outputFullPath -Force
}

$staging = Join-Path $root ".pack-staging"
if (Test-Path -LiteralPath $staging) {
    Remove-Item -LiteralPath $staging -Recurse -Force
}
New-Item -ItemType Directory -Path $staging | Out-Null

$excludeDirNames = @(".venv", "__pycache__", ".pytest_cache", ".git")
$excludeFilePatterns = @("*.pyc", "*.pyo", "*.pyd")

Get-ChildItem -LiteralPath $root -Recurse -Force | ForEach-Object {
    $item = $_

    if ($item.FullName -like "$staging*") {
        return
    }

    $relative = $item.FullName.Substring($root.Length).TrimStart('\\')
    if ([string]::IsNullOrWhiteSpace($relative)) {
        return
    }

    $segments = $relative -split '\\'
    foreach ($name in $excludeDirNames) {
        if ($segments -contains $name) {
            return
        }
    }

    if (-not $item.PSIsContainer) {
        foreach ($pattern in $excludeFilePatterns) {
            if ($item.Name -like $pattern) {
                return
            }
        }
    }

    $dest = Join-Path $staging $relative
    if ($item.PSIsContainer) {
        if (-not (Test-Path -LiteralPath $dest)) {
            New-Item -ItemType Directory -Path $dest | Out-Null
        }
    }
    else {
        $parent = Split-Path -Parent $dest
        if (-not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent | Out-Null
        }
        Copy-Item -LiteralPath $item.FullName -Destination $dest -Force
    }
}

Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $outputFullPath -Force
Remove-Item -LiteralPath $staging -Recurse -Force
Write-Output "Created package: $outputFullPath"
