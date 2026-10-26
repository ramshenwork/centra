<#
.SYNOPSIS
  Copies a new release into releases/<Version>/, builds its venv and installs
  dependencies. Does NOT approve or activate it (approve-release.ps1, then
  activate-release.ps1).

  Fix vs the STRUCT template: if the source folder's parent contains a
  release.json, it is copied as-is so your release notes are kept. Only when
  there is none is a minimal one written.

.PARAMETER Version     New release folder name, e.g. "1.0.1" (RR decides version numbers).
.PARAMETER SourcePath  Path to the new release's app\ folder.
#>
param(
    [Parameter(Mandatory=$true)][string]$Version,
    [Parameter(Mandatory=$true)][string]$SourcePath
)
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReleaseDir = Join-Path $ProjectRoot "releases\$Version"
if (Test-Path $ReleaseDir) { Write-Error "Release '$Version' already exists at $ReleaseDir - choose a new version number."; exit 1 }

New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
Copy-Item -Path $SourcePath -Destination (Join-Path $ReleaseDir "app") -Recurse

Write-Host "[deploy] Creating venv for $Version ..."
python -m venv (Join-Path $ReleaseDir "venv")
& (Join-Path $ReleaseDir "venv\Scripts\pip.exe") install -r (Join-Path $ReleaseDir "app\backend\requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Error "Dependency install failed."; exit 1 }

$SourceReleaseJson = Join-Path (Split-Path -Parent $SourcePath) "release.json"
if (Test-Path $SourceReleaseJson) {
    Copy-Item $SourceReleaseJson (Join-Path $ReleaseDir "release.json")
} else {
    @{ version = $Version; built_from = $SourcePath; schema_version = 1 } | ConvertTo-Json | Set-Content (Join-Path $ReleaseDir "release.json")
}
@{ status = "built"; deployed_at = $null; approved_at = $null; approved_by = $null; preflight_passed = $null } |
    ConvertTo-Json | Set-Content (Join-Path $ReleaseDir "deployment-status.json")
Write-Host "[deploy] $Version staged at $ReleaseDir. Run approve-release.ps1 -Version $Version next."
