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
if (-not (Test-Path -LiteralPath (Join-Path $root 'jasna.exe') -PathType Leaf)) { throw 'InstallDir must directly contain jasna.exe.' }
if (Get-Process -Name 'jasna' -ErrorAction SilentlyContinue) { throw 'Close Jasna before applying the patch.' }
Write-Host 'Verifying the complete v0.11.1 installation...'
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
Write-Host 'Verifying the complete v0.11.2 installation...'
Verify-Manifest $new $root
foreach ($relative in @($diff.deleted)) {
    if ($null -eq $new.PSObject.Properties[$relative] -and (Test-Path -LiteralPath (Safe-Path $root $relative) -PathType Leaf)) { throw "Obsolete file remains: $relative" }
}
Write-Host 'Success: Jasna updated from v0.11.1 to v0.11.2.'
