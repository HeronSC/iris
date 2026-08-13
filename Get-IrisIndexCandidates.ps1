[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    [string[]]$SupportedExtensions = @('.docx', '.xlsx', '.xls', '.pdf', '.txt', '.md', '.csv'),

    [string[]]$ExcludedDirectories = @('.git', '.venv', 'node_modules', '__pycache__', 'bin', 'obj'),

    [string[]]$ExcludedDirectoryPrefixes = @('.'),

    [switch]$SummaryOnly
)

function Get-IrisIndexCandidates {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string[]]$SupportedExtensions,

        [Parameter(Mandatory = $true)]
        [string[]]$ExcludedDirectories,

        [Parameter(Mandatory = $true)]
        [string[]]$ExcludedDirectoryPrefixes
    )

    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    $extensionSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($extension in $SupportedExtensions) {
        if ([string]::IsNullOrWhiteSpace($extension)) {
            continue
        }

        if ($extension.StartsWith('.')) {
            [void]$extensionSet.Add($extension)
        }
        else {
            [void]$extensionSet.Add('.' + $extension)
        }
    }

    $excludedSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($directoryName in $ExcludedDirectories) {
        if (-not [string]::IsNullOrWhiteSpace($directoryName)) {
            [void]$excludedSet.Add($directoryName)
        }
    }

    $excludedPrefixes = [System.Collections.Generic.List[string]]::new()
    foreach ($prefix in $ExcludedDirectoryPrefixes) {
        if (-not [string]::IsNullOrWhiteSpace($prefix)) {
            [void]$excludedPrefixes.Add($prefix.ToLowerInvariant())
        }
    }

    $pending = [System.Collections.Generic.Stack[System.IO.DirectoryInfo]]::new()
    $pending.Push([System.IO.DirectoryInfo]::new($resolvedRoot))

    while ($pending.Count -gt 0) {
        $current = $pending.Pop()

        try {
            foreach ($entry in $current.EnumerateFileSystemInfos()) {
                if ($entry.Attributes -band [System.IO.FileAttributes]::Directory) {
                    $entryNameLower = $entry.Name.ToLowerInvariant()
                    $isExcludedByPrefix = $false
                    foreach ($prefix in $excludedPrefixes) {
                        if ($entryNameLower.StartsWith($prefix)) {
                            $isExcludedByPrefix = $true
                            break
                        }
                    }

                    if (-not $excludedSet.Contains($entry.Name) -and -not $isExcludedByPrefix) {
                        $pending.Push([System.IO.DirectoryInfo]$entry)
                    }
                    continue
                }

                $extension = [System.IO.Path]::GetExtension($entry.Name)
                if (-not $extensionSet.Contains($extension)) {
                    continue
                }

                [pscustomobject]@{
                    FullName         = $entry.FullName
                    Extension        = $extension.ToLowerInvariant()
                    Length           = $entry.Length
                    LastWriteTimeUtc = $entry.LastWriteTimeUtc
                }
            }
        }
        catch {
            Write-Warning ("Skipping {0}: {1}" -f $current.FullName, $_.Exception.Message)
        }
    }
}

$matches = @(Get-IrisIndexCandidates -Root $Root -SupportedExtensions $SupportedExtensions -ExcludedDirectories $ExcludedDirectories -ExcludedDirectoryPrefixes $ExcludedDirectoryPrefixes)

if ($SummaryOnly) {
    $summary = $matches |
        Group-Object Extension |
        Sort-Object Name |
        Select-Object Name, Count

    $summary
    [pscustomobject]@{
        Root           = (Resolve-Path -LiteralPath $Root).Path
        CandidateCount = $matches.Count
    }
}
else {
    $matches
}