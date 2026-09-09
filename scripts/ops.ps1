<#
.SYNOPSIS
  Bring the semigraph demo online / offline / show status (Windows PowerShell 5.1+).

.USAGE
  .\scripts\ops.ps1 start    # DB machine up -> deploy API -> kill switch off -> URL
  .\scripts\ops.ps1 stop     # kill switch on -> API scaled to 0 -> DB machine stopped
  .\scripts\ops.ps1 status   # both apps, health, kill switch, spend ledger

  Order matters: START brings the database up before the API (the API connects on
  boot) and re-enables live questions last; STOP silences live questions first,
  then removes the API machine, then stops the database. Everything else (volume,
  secrets, the baked graph seed) persists. Both stopped ≈ $0.75/month.
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet("start", "stop", "status")][string]$Action
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$ApiApp = "semigraph"
$DbApp = "semigraph-neo4j"

function Get-DbMachineIds {
  (flyctl machines list -a $DbApp --json | ConvertFrom-Json) | ForEach-Object { $_.id }
}

function Wait-Health($url, $seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15
      if ($r.StatusCode -eq 200) { return $true }
    } catch { }
    Start-Sleep -Seconds 5
  }
  return $false
}

switch ($Action) {
  "start" {
    Write-Host "1/4 starting the Neo4j machine ($DbApp)..."
    foreach ($id in Get-DbMachineIds) { flyctl machine start $id -a $DbApp }
    Write-Host "2/4 deploying the API ($ApiApp) - remote build, ~2-15 min..."
    flyctl deploy --ha=false --remote-only --yes
    Write-Host "3/4 waiting for /healthz..."
    if (-not (Wait-Health "https://$ApiApp.fly.dev/healthz" 180)) { throw "API did not become healthy" }
    Write-Host "4/4 re-enabling live questions..."
    & $Py -m scripts.kill_switch off
    Write-Host "ONLINE: https://$ApiApp.fly.dev/"
  }
  "stop" {
    Write-Host "1/3 kill switch on (live questions decline; cached answers keep working)..."
    try { & $Py -m scripts.kill_switch on } catch { Write-Warning "kill switch call failed (API already down?) - continuing" }
    Write-Host "2/3 removing the API machine ($ApiApp)..."
    flyctl scale count 0 -a $ApiApp --yes
    Write-Host "3/3 stopping the Neo4j machine ($DbApp) - volume + data persist..."
    foreach ($id in Get-DbMachineIds) { flyctl machine stop $id -a $DbApp }
    Write-Host "OFFLINE. Remaining cost: rootfs + 3 GB volume (about 0.75 USD/month)."
  }
  "status" {
    flyctl status -a $ApiApp
    flyctl status -a $DbApp
    try { (Invoke-WebRequest -Uri "https://$ApiApp.fly.dev/healthz" -UseBasicParsing -TimeoutSec 30).Content } catch { Write-Host "healthz: $($_.Exception.Message)" }
    try { & $Py -m scripts.kill_switch status } catch { Write-Host "kill switch: $($_.Exception.Message)" }
  }
}
