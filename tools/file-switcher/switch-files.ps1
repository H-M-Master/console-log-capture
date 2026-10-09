#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Mode,
    [switch]$Status,
    [switch]$DryRun,
    [switch]$AllowUnknown,
    [ValidateSet('Text', 'Json')]
    [string]$OutputFormat = 'Text',
    [string]$Config,
    [string]$StateRoot
)

$ErrorActionPreference = 'Stop'
$mutex = $null
$locked = $false
$transaction = $null
$backupReady = $false
$cacheMoves = @()
$stagedFiles = @()
$originalHashes = @{}
$journal = $null
$action = if ($Status) { 'status' } elseif ($Mode) { 'switch' } else { 'unknown' }

function Get-Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant() }
function Resolve-ConfiguredPath([string]$Path, [string]$Base) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'Configuration contains an empty path.' }
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $Base $Path))
}
function Assert-PlainFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "File does not exist: $Path" }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Reparse-point files are not allowed: $Path" }
}
function Test-CreatorRunning { return @((Get-Process -Name CocosCreator,Creator -ErrorAction SilentlyContinue)).Count -gt 0 }
function Save-Journal {
    if ($null -ne $journal -and $null -ne $transaction) { $journal | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $transaction 'journal.json') -Encoding UTF8 }
}
function Get-CacheInfo([object[]]$Paths) {
    $result = @()
    foreach ($path in $Paths) {
        $exists = Test-Path -LiteralPath $path -PathType Container
        $result += [ordered]@{ path = $path; exists = [bool]$exists; type = if (Test-Path -LiteralPath $path) { if ($exists) { 'directory' } else { 'other' } } else { 'missing' } }
    }
    return $result
}
function Write-JsonEnvelope([bool]$Ok, [object]$Data, [string]$ErrorCode, [string]$ErrorMessage) {
    $envelope = [ordered]@{ schemaVersion = 1; ok = $Ok; action = $action; data = $Data; transaction = if ($null -ne $journal) { $journal } else { $null }; error = if ($ErrorCode) { [ordered]@{ code = $ErrorCode; message = $ErrorMessage } } else { $null } }
    Write-Output ($envelope | ConvertTo-Json -Depth 16 -Compress)
}

try {
    if (-not $Config) { $Config = Join-Path $PSScriptRoot 'config.json' }
    if (-not $StateRoot) { $StateRoot = Join-Path $PSScriptRoot 'state' }
    $mutex = New-Object -TypeName System.Threading.Mutex -ArgumentList $false, 'Local\AnyTestTools.FileSwitcher'
    $locked = $mutex.WaitOne(0)
    if (-not $locked) { throw 'Another file-switch operation is already running.' }
    Assert-PlainFile $Config
    $settings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($settings.schemaVersion -ne 1) { throw 'Unsupported config schemaVersion. Expected 1.' }
    if (-not $settings.targetRoot -or -not $settings.modes -or -not $settings.entries) { throw 'Config must contain targetRoot, modes, and entries.' }
    $targetRoot = Resolve-ConfiguredPath ([string]$settings.targetRoot) $PSScriptRoot
    if (-not (Test-Path -LiteralPath $targetRoot -PathType Container)) { throw "Target root does not exist: $targetRoot" }
    $modeNames = @($settings.modes.PSObject.Properties.Name)
    if ($modeNames.Count -lt 2) { throw 'Configure at least two modes.' }
    if (@($settings.entries).Count -eq 0) { throw 'entries must not be empty.' }
    if (-not $Status -and -not $Mode) { throw 'Use -Mode <name> or -Status.' }
    if ($Mode -and $modeNames -cnotcontains $Mode) { throw "Unknown mode: $Mode. Available: $($modeNames -join ', ')" }
    if ($Status -and $Mode) { throw 'Do not use -Status and -Mode together.' }

    $items = @()
    $seen = New-Object -TypeName 'System.Collections.Generic.HashSet[string]' -ArgumentList ([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in @($settings.entries)) {
        $relative = [string]$entry.target
        if (-not $relative -or [IO.Path]::IsPathRooted($relative)) { throw "target must be relative: $relative" }
        $destination = Resolve-ConfiguredPath $relative $targetRoot
        $prefix = $targetRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        if (-not $destination.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Target is outside targetRoot: $relative" }
        if (-not $seen.Add($destination)) { throw "Duplicate target: $destination" }
        Assert-PlainFile $destination
        $sourcePaths = @{}
        $sourceHashes = @{}
        foreach ($name in $modeNames) {
            $sourcePath = Resolve-ConfiguredPath ([string]$entry.sources.$name) $PSScriptRoot
            Assert-PlainFile $sourcePath
            $sourcePaths[$name] = $sourcePath
            $sourceHashes[$name] = Get-Hash $sourcePath
        }
        $items += [pscustomobject]@{ RelativeTarget = $relative; Target = $destination; Sources = $sourcePaths; Hashes = $sourceHashes; Current = Get-Hash $destination }
    }
    $currentMode = $null
    foreach ($name in $modeNames) {
        $matches = @($items | Where-Object { $_.Current -eq $_.Hashes[$name] })
        if ($matches.Count -eq $items.Count) { if ($null -ne $currentMode) { $currentMode = 'ambiguous'; break }; $currentMode = $name }
    }
    if (-not $currentMode) { $currentMode = 'unknown/mixed' }
    $cachePaths = @()
    foreach ($value in @($settings.cacheDirectories)) {
        $cachePath = Resolve-ConfiguredPath ([string]$value) $targetRoot
        if ($cachePaths -contains $cachePath) { throw "Duplicate cache directory: $cachePath" }
        if ((Test-Path -LiteralPath $cachePath) -and -not (Test-Path -LiteralPath $cachePath -PathType Container)) { throw "Cache path is not a directory: $cachePath" }
        $cachePaths += $cachePath
    }
    $entriesData = @($items | ForEach-Object { [ordered]@{ target = $_.RelativeTarget; resolvedTarget = $_.Target; currentHash = $_.Current; hashes = $_.Hashes } })
    $statusData = [ordered]@{ currentMode = $currentMode; modeNames = $modeNames; targetRoot = $targetRoot; fileCount = $items.Count; entries = $entriesData; creatorRunning = Test-CreatorRunning; cacheDirectories = @(Get-CacheInfo $cachePaths) }
    if ($Status) {
        if ($OutputFormat -eq 'Json') { Write-JsonEnvelope $true $statusData $null $null; return }
        Write-Host "Current mode: $currentMode; file count: $($items.Count)"; foreach ($item in $items) { Write-Host "  $($item.Target) [$($item.Current.Substring(0, 12))]" }; return
    }
    if ($OutputFormat -eq 'Text') { Write-Host "Current mode: $currentMode; file count: $($items.Count)"; foreach ($item in $items) { Write-Host "  $($item.Target) [$($item.Current.Substring(0, 12))]" } }
    if (Test-CreatorRunning) { throw 'Cocos Creator is running. Close it before switching.' }
    if ($currentMode -eq $Mode) { if ($OutputFormat -eq 'Json') { Write-JsonEnvelope $true ([ordered]@{ currentMode = $currentMode; targetMode = $Mode; changed = $false }) $null $null } else { Write-Host "Already in mode $Mode. Nothing to do." }; return }
    if (($currentMode -eq 'unknown/mixed' -or $currentMode -eq 'ambiguous') -and -not $AllowUnknown) { throw 'Current files do not exactly match a configured mode. Inspect them first, or explicitly use -AllowUnknown.' }
    if ($OutputFormat -eq 'Text') { Write-Host "Target mode: $Mode; cache directories: $($cachePaths.Count)" }
    if ($DryRun) {
        $dryData = [ordered]@{ currentMode = $currentMode; targetMode = $Mode; changed = $true; fileCount = $items.Count; cacheDirectories = @(Get-CacheInfo $cachePaths) }
        if ($OutputFormat -eq 'Json') { Write-JsonEnvelope $true $dryData $null $null } else { Write-Host 'Dry run complete. No files, backups, or caches were changed.' }; return
    }
    $StateRoot = [IO.Path]::GetFullPath($StateRoot)
    if (([IO.Path]::GetPathRoot($StateRoot)) -ne ([IO.Path]::GetPathRoot($targetRoot))) { throw 'StateRoot and targetRoot must be on the same drive.' }
    $id = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
    $transaction = Join-Path $StateRoot $id; $backupDir = Join-Path $transaction 'originals'; $cacheDir = Join-Path $transaction 'caches'
    New-Item -ItemType Directory -Path $backupDir, $cacheDir -Force | Out-Null
    $journal = [ordered]@{ id = $id; from = $currentMode; to = $Mode; status = 'started'; files = @(); caches = @() }; Save-Journal
    $index = 0
    foreach ($item in $items) {
        $backup = Join-Path $backupDir ("{0:D4}.bak" -f $index); Copy-Item -LiteralPath $item.Target -Destination $backup
        if ((Get-Hash $backup) -ne $item.Current) { throw "Backup verification failed: $($item.Target)" }
        $originalHashes[$item.Target] = $item.Current; $journal.files += @{ target = $item.Target; backup = $backup; originalHash = $item.Current; newHash = $item.Hashes[$Mode] }; $index++
    }
    $backupReady = $true; Save-Journal
    $index = 0
    foreach ($cachePath in $cachePaths) {
        if (Test-Path -LiteralPath $cachePath -PathType Container) { $saved = Join-Path $cacheDir ("{0:D4}" -f $index); Move-Item -LiteralPath $cachePath -Destination $saved; $cacheMoves += [pscustomobject]@{ Original = $cachePath; Saved = $saved }; $journal.caches += @{ original = $cachePath; saved = $saved }; Save-Journal }; $index++
    }
    foreach ($item in $items) {
        $temp = Join-Path (Split-Path -Parent $item.Target) ('.anytesttools-' + $id + '-' + [IO.Path]::GetFileName($item.Target) + '.tmp')
        if (Test-Path -LiteralPath $temp) { throw "Temporary file already exists: $temp" }; $stagedFiles += $temp; Copy-Item -LiteralPath $item.Sources[$Mode] -Destination $temp
        if ((Get-Hash $temp) -ne $item.Hashes[$Mode]) { throw "Temporary file verification failed: $temp" }
    }
    $index = 0
    foreach ($item in $items) {
        $originalAside = Join-Path (Split-Path -Parent $item.Target) ('.anytesttools-' + $id + '-' + [IO.Path]::GetFileName($item.Target) + '.old'); Move-Item -LiteralPath $item.Target -Destination $originalAside
        try { Move-Item -LiteralPath $stagedFiles[$index] -Destination $item.Target } catch { Move-Item -LiteralPath $originalAside -Destination $item.Target; throw }; Remove-Item -LiteralPath $originalAside -Force; $index++
    }
    foreach ($item in $items) { if ((Get-Hash $item.Target) -ne $item.Hashes[$Mode]) { throw "Post-switch verification failed: $($item.Target)" } }
    $journal.status = 'completed'; Save-Journal
    $resultData = [ordered]@{ currentMode = $currentMode; targetMode = $Mode; changed = $true; fileCount = $items.Count }
    if ($OutputFormat -eq 'Json') { Write-JsonEnvelope $true $resultData $null $null } else { Write-Host "Switched to $Mode. Transaction backup: $transaction"; Write-Host 'Start Cocos Creator again so it can rebuild its caches.' }
} catch {
    $message = $_.Exception.Message
    if ($OutputFormat -eq 'Json') { [Console]::Error.WriteLine($message) } else { Write-Error -Message $message -ErrorAction Continue }
    if ($transaction -and $backupReady) {
        $rollbackErrors = @()
        foreach ($file in $journal.files) { try { $target = [string]$file.target; if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }; Copy-Item -LiteralPath ([string]$file.backup) -Destination $target; if ((Get-Hash $target) -ne $originalHashes[$target]) { throw "Rollback verification failed: $target" } } catch { $rollbackErrors += $_.Exception.Message } }
        for ($i = $cacheMoves.Count - 1; $i -ge 0; $i--) { try { $cache = $cacheMoves[$i]; if (Test-Path -LiteralPath $cache.Original) { throw "Cache original path is occupied: $($cache.Original)" }; Move-Item -LiteralPath $cache.Saved -Destination $cache.Original } catch { $rollbackErrors += $_.Exception.Message } }
        if ($rollbackErrors.Count -eq 0) { $journal.status = 'rolled-back' } else { $journal.status = 'recovery-required' }; Save-Journal
    }
    if ($OutputFormat -eq 'Json') { Write-JsonEnvelope $false $null 'operation-failed' $message }
    exit 1
} finally {
    foreach ($temp in $stagedFiles) { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue } }
    if ($locked) { $mutex.ReleaseMutex() }; if ($mutex) { $mutex.Dispose() }
}
