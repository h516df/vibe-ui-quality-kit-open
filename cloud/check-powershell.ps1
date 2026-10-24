$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$files = git ls-files -- '*.ps1'
if ($LASTEXITCODE -ne 0) { throw 'Cannot enumerate PowerShell sources' }
foreach ($file in $files) {
    if ($file -match '^(snapshots|recovery)/') { continue }
    $errors = $null; $tokens = $null
    [Management.Automation.Language.Parser]::ParseFile((Join-Path $root $file), [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count) { throw "PowerShell syntax failed: $file" }
}
Write-Output 'PowerShell source syntax checked; no host configuration was executed.'
