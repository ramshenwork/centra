<#
.SYNOPSIS
  Flips state/active-release.json back to the release that was active
  immediately before the current one — the exact inverse of
  activate-release.ps1. No code is touched; this only ever changes
  the pointer.
#>

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PointerFile = Join-Path $ProjectRoot "state\active-release.json"

$Pointer = Get-Content $PointerFile | ConvertFrom-Json
if (-not $Pointer.previous_release) {
    Write-Error "No previous_release recorded in $PointerFile — nothing to roll back to."
    exit 1
}

$RollingBackTo = $Pointer.previous_release
$RollingBackFrom = $Pointer.active_release

$NewPointer = @{
    active_release   = $RollingBackTo
    activated_at     = (Get-Date).ToString("o")
    activated_by     = $env:USERNAME
    previous_release = $RollingBackFrom
}
$NewPointer | ConvertTo-Json | Set-Content $PointerFile

Write-Host "[rollback] $RollingBackFrom -> $RollingBackTo. Restart the launcher process(es) to pick this up."
