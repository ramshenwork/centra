<#
.SYNOPSIS
  Runs the preflight tests against a release. Only a green run marks it
  approved; activate-release.ps1 refuses anything unapproved. Uses the
  release's own venv Python when it exists.
.PARAMETER Version  Release folder name under releases\, e.g. "1.0.1"
#>
param([Parameter(Mandatory=$true)][string]$Version)
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReleaseDir = Join-Path $ProjectRoot "releases\$Version"
$BackendDir = Join-Path $ReleaseDir "app\backend"
$StatusFile = Join-Path $ReleaseDir "deployment-status.json"
if (-not (Test-Path $ReleaseDir)) { Write-Error "Release '$Version' does not exist at $ReleaseDir"; exit 1 }
$Py = Join-Path $ReleaseDir "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

Write-Host "[approve-release] Running preflight suite against $Version ..."
Push-Location $BackendDir
& $Py -m pytest tests/preflight -v
$ExitCode = $LASTEXITCODE
Pop-Location

$Status = Get-Content $StatusFile | ConvertFrom-Json
if ($ExitCode -eq 0) {
    Write-Host "[approve-release] PASSED - marking $Version approved."
    $Status.preflight_passed = $true
    $Status.approved_at = (Get-Date).ToString("o")
    $Status.approved_by = $env:USERNAME
} else {
    Write-Host "[approve-release] FAILED - $Version remains unapproved." -ForegroundColor Red
    $Status.preflight_passed = $false
}
$Status | ConvertTo-Json | Set-Content $StatusFile
exit $ExitCode
