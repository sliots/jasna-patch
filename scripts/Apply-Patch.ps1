param([Parameter(Mandatory=$true)][string]$InstallDir)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = (Resolve-Path -LiteralPath $InstallDir).Path
$patch = $PSScriptRoot
$old = Get-Content -LiteralPath (Join-Path $patch 'old-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$new = Get-Content -LiteralPath (Join-Path $patch 'new-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$diff = Get-Content -LiteralPath (Join-Path $patch 'diff.json') -Raw -Encoding UTF8 | ConvertFrom-Json
function Safe-Path([string]$base, [string]$relative) {
    if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[/\\])\.\.([/\\]|$)' -or $relative.Contains(':')) { throw "Unsafe relative path: $relative" }
    $full = [IO.Path]::GetFullPath((Join-Path $base $relative))
    if (-not $full.StartsWith($base.TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw "Path escapes installation: $relative" }
    $parent = $full
    while ($parent -and $parent.Length -ge $base.Length) {
        if (Test-Path -LiteralPath $parent) {
            if ((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point not allowed: $parent" }
        }
        if ($parent -eq $base) { break }
        $parent = Split-Path -Parent $parent
    }
    return $full
}
function Verify-Manifest($manifest, [string]$base) {
    foreach ($entry in $manifest.PSObject.Properties) {
        $file = Safe-Path $base $entry.Name
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing file: $($entry.Name)" }
        if ((Get-Item -LiteralPath $file).Length -ne $entry.Value.size) { throw "Size mismatch: $($entry.Name)" }
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $entry.Value.sha256) { throw "SHA256 mismatch: $($entry.Name)" }
    }
}
# Patches without this field were produced by the original Jasna-only builder.
$markerProperty = $diff.PSObject.Properties['entry_file']
$entryFile = if ($null -ne $markerProperty) { [string]$markerProperty.Value } else { 'jasna.exe' }
if ($entryFile) {
    if (-not (Test-Path -LiteralPath (Safe-Path $root $entryFile) -PathType Leaf)) { throw "Installation root must contain $entryFile" }
    if ([IO.Path]::GetExtension($entryFile) -ieq '.exe') {
        $processName = [IO.Path]::GetFileNameWithoutExtension($entryFile)
        if (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -eq $processName }) { throw "Close $processName before applying the patch." }
    }
}
Write-Host "Verifying baseline files ($($diff.from))..."
Verify-Manifest $old $root
$payloadRoot = Join-Path $patch 'payload'
foreach ($relative in @($diff.added) + @($diff.modified)) {
    $source = Safe-Path $payloadRoot $relative
    $dest = Safe-Path $root $relative
    $expected = $new.PSObject.Properties[$relative].Value
    if ((Get-Item -LiteralPath $source).Length -ne $expected.size -or (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $expected.sha256) { throw "Corrupt patch payload: $relative" }
    if ($relative -in @($diff.added) -and (Test-Path -LiteralPath $dest)) { throw "New file conflicts with existing user file: $relative" }
}
Write-Host 'Applying patch. Keep your backup until verification succeeds.'
# Remove obsolete files first to support file/directory transitions.
foreach ($relative in @($diff.deleted)) {
    $dest = Safe-Path $root $relative
    Remove-Item -LiteralPath $dest -Force
}
foreach ($relative in @($diff.added) + @($diff.modified)) {
    $source = Safe-Path $payloadRoot $relative
    $dest = Safe-Path $root $relative
    $parent = Split-Path -Parent $dest
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Copy-Item -LiteralPath $source -Destination $dest -Force
}
Write-Host "Verifying target files ($($diff.to))..."
Verify-Manifest $new $root
foreach ($relative in @($diff.deleted)) {
    if ($null -eq $new.PSObject.Properties[$relative] -and (Test-Path -LiteralPath (Safe-Path $root $relative) -PathType Leaf)) { throw "Obsolete file remains: $relative" }
}
Write-Host "Success: updated from $($diff.from) to $($diff.to)."
