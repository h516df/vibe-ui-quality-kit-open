[CmdletBinding()]
param(
  [switch]$RegisterDsh,
  [string]$ExpectedCheckpoint,
  [string]$CheckpointRepository = 'h516df/vibe-ui-quality-kit',
  [string]$ExpectedRecoveryBlob,
  [string]$ArtifactRoot = $PSScriptRoot,
  # 安装根目录：默认放当前用户的 LOCALAPPDATA，可用 -InstallRoot 覆盖。
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Impeccable\4.6.0-5bf0a069'),
  # DSH 主目录：注册 skill 时用；默认读 DSH_HOME 环境变量。
  [string]$DshHome = $env:DSH_HOME,
  # 需要的代理：默认沿用环境变量 HTTPS_PROXY，未设置则直连。
  [string]$Proxy = $env:HTTPS_PROXY
)
$ErrorActionPreference = 'Stop'
$installRoot = $InstallRoot
$engineRoot = Join-Path $installRoot 'engine-0.1.15'
$engineHash = '981324EA15CC3C107D46B8133B5BE77F639DBCD22E671F1EB7D8C69136E9693B'
$engineSource = Join-Path $ArtifactRoot 'impeccable-windows-x64.exe'
if (-not (Test-Path -LiteralPath $engineSource)) {
  $download = @{
    Uri         = 'https://github.com/pbakaus/impeccable/releases/download/engine-v0.1.15/impeccable-windows-x64.exe'
    TimeoutSec  = 120
    OutFile     = $engineSource
  }
  if ($Proxy) { $download.Proxy = $Proxy }
  Invoke-WebRequest @download
}

function Add-VerifiedJunction([string]$LinkPath, [string]$TargetPath) {
  if (Test-Path -LiteralPath $LinkPath) {
    $item = Get-Item -LiteralPath $LinkPath -Force
    if ($item.LinkType -ne 'Junction' -or [IO.Path]::GetFullPath([string]$item.Target).TrimEnd('\') -ne [IO.Path]::GetFullPath($TargetPath).TrimEnd('\')) {
      throw "Refusing to replace an existing path: $LinkPath"
    }
    return
  }
  New-Item -ItemType Directory -Path (Split-Path -Parent $LinkPath) -Force | Out-Null
  New-Item -ItemType Junction -Path $LinkPath -Target $TargetPath | Out-Null
}

if ((Get-FileHash -LiteralPath $engineSource -Algorithm SHA256).Hash -ne $engineHash) {
  throw 'The engine does not match the official release SHA256.'
}
if ($RegisterDsh) {
  if ($ExpectedCheckpoint -notmatch '^[0-9a-f]{40}$') { throw 'A verified checkpoint SHA is required.' }
  if ($ExpectedRecoveryBlob -notmatch '^[0-9a-f]{40}$') { throw 'A verified recovery manifest blob SHA is required.' }
  $env:HTTPS_PROXY = $null
  $env:HTTP_PROXY = $null
  $env:ALL_PROXY = $null
  $env:NO_PROXY = 'api.github.com'
  $private = & gh api "repos/$CheckpointRepository" --jq .private
  if ($LASTEXITCODE -ne 0 -or $private -ne 'true') { throw 'The recovery repository is not confirmed private.' }
  $remoteBlob = & gh api "repos/$CheckpointRepository/contents/recovery/manifest.json?ref=$ExpectedCheckpoint" --jq .sha
  if ($LASTEXITCODE -ne 0 -or $remoteBlob -ne $ExpectedRecoveryBlob) {
    throw 'DSH registration refused: the archived checkpoint does not match its verified Git blob digest.'
  }
}
New-Item -ItemType Directory -Path $engineRoot -Force | Out-Null
$engineDest = Join-Path $engineRoot 'impeccable.exe'
if (Test-Path -LiteralPath $engineDest) {
  if ((Get-FileHash -LiteralPath $engineDest -Algorithm SHA256).Hash -ne $engineHash) { throw 'Existing engine differs.' }
} else {
  Copy-Item -LiteralPath $engineSource -Destination $engineDest
}
foreach ($provider in @('codex', 'dsh')) {
  $source = Join-Path $ArtifactRoot "staged-$provider\impeccable"
  $dest = Join-Path $installRoot "$provider\impeccable"
  if (-not (Test-Path -LiteralPath $dest)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $dest -Recurse
  }
  foreach ($file in Get-ChildItem -LiteralPath $source -Recurse -File) {
    $relative = [IO.Path]::GetRelativePath($source, $file.FullName)
    $installed = Join-Path $dest $relative
    if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash) {
      throw "Installed upstream file differs: $provider/$relative"
    }
  }
  Add-VerifiedJunction (Join-Path $dest 'scripts\bin\windows-x64') $engineRoot
}
Copy-Item -LiteralPath (Join-Path $ArtifactRoot 'LICENSE-impeccable.txt') -Destination (Join-Path $installRoot 'LICENSE.txt')
$sourceMetadata = [ordered]@{
  repository = 'https://github.com/pbakaus/impeccable'
  commit = '5bf0a069dc857e1b5f4b090e004d9d5b231416ca'
  skillVersion = '4.6.0'
  engineVersion = '0.1.15'
  engineSHA256 = $engineHash
  officialProviderPaths = @('.agents/skills/impeccable', '.dsh/skills/impeccable')
  automaticHooksInstalled = $false
  sourceFilesModified = $false
}
$sourceMetadata | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $installRoot 'SOURCE.json') -Encoding utf8
Add-VerifiedJunction (Join-Path $env:USERPROFILE '.codex\skills\impeccable') (Join-Path $installRoot 'codex\impeccable')
if ($RegisterDsh) {
  if (-not $DshHome) { throw 'DSH registration needs -DshHome or the DSH_HOME environment variable.' }
  Add-VerifiedJunction (Join-Path $DshHome 'skills\impeccable') (Join-Path $installRoot 'dsh\impeccable')
}
[ordered]@{installedAt=$installRoot; codexRegistered=$true; dshRegistered=[bool]$RegisterDsh; engineSHA256=$engineHash} | ConvertTo-Json
