<#
.SYNOPSIS
  Flips state/active-release.json to point at a new, already-approved
  release. Refuses to activate a release that hasn't passed
  approve-release.ps1's preflight gate. This — not a code deploy step —
  is the actual "go live" moment: bin/launcher.py reads this pointer
  on every process start.
#>
param(
    [Parameter(Mandatory=$true)][string]$Version
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReleaseDir = Join-Path $ProjectRoot "releases\$Version"
$StatusFile = Join-Path $ReleaseDir "deployment-status.json"
$PointerFile = Join-Path $ProjectRoot "state\active-release.json"

if (-not (Test-Path $ReleaseDir)) {
    Write-Error "Release '$Version' does not exist at $ReleaseDir"
    exit 1
}

$Status = Get-Content $StatusFile | ConvertFrom-Json
if (-not $Status.preflight_passed) {
    Write-Error "Release '$Version' has not passed preflight (run approve-release.ps1 first). Refusing to activate."
    exit 1
}

$Pointer = Get-Content $PointerFile | ConvertFrom-Json
$Previous = $Pointer.active_release

$NewPointer = @{
    active_release   = $Version
    activated_at     = (Get-Date).ToString("o")
    activated_by     = $env:USERNAME
    previous_release = $Previous
}
$NewPointer | ConvertTo-Json | Set-Content $PointerFile

$Status.status = "active"
$Status.deployed_at = (Get-Date).ToString("o")
$Status | ConvertTo-Json | Set-Content (Join-Path $ReleaseDir "deployment-status.json")

Write-Host "[activate-release] $Previous -> $Version. Restart the launcher process(es) to pick this up."
