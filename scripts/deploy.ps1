# NexusQA one-command deploy
# Usage:
#   .\scripts\deploy.ps1                        # deploy all 3 services
#   .\scripts\deploy.ps1 qe-central             # deploy only qe-central
#   .\scripts\deploy.ps1 platform-api           # deploy only platform-api
#   .\scripts\deploy.ps1 -PushOnly              # just push, don't deploy to VM

param(
    [switch]$PushOnly,
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$Services
)

$ErrorActionPreference = "Stop"

$VM_NAME     = "verdict-box"
$VM_ZONE     = "asia-southeast1-a"
$VM_PROJECT  = "project-8d85a07a-396c-40aa-9b6"
$VM_USER     = "srika"
$VM_SRC      = "/home/$VM_USER/nexus-src"
$REMOTE      = "mine"
$BRANCH      = "develop"

$QEC_COMPOSE  = "docker-compose.qec.yml"
$MAIN_COMPOSE = "docker-compose.yml"

$ValidServices = @("qe-central", "qe-explorer", "platform-api")

foreach ($svc in $Services) {
    if ($svc -notin $ValidServices) {
        Write-Host "Unknown service: $svc. Valid: $($ValidServices -join ', ')" -ForegroundColor Red
        exit 1
    }
}

if (-not $Services -or $Services.Count -eq 0) {
    $Services = $ValidServices
    Write-Host "Deploying all services: $($Services -join ', ')" -ForegroundColor Cyan
} else {
    Write-Host "Deploying: $($Services -join ', ')" -ForegroundColor Cyan
}

# Step 1: Push
Write-Host "`n[1/3] Pushing to $REMOTE/$BRANCH..." -ForegroundColor Blue
Set-Location $PSScriptRoot\..
# git writes ordinary progress ("To github.com:...") to STDERR. Under
# $ErrorActionPreference='Stop' PowerShell 5.1 promotes that to a TERMINATING
# error, so a SUCCESSFUL push aborted the deploy before $LASTEXITCODE was even
# read. Only the exit code decides success here.
$ErrorActionPreference = "Continue"
git push $REMOTE $BRANCH
$pushExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($pushExit -ne 0) {
    Write-Host "Push failed. Check SSH key: ssh -T git@github.com" -ForegroundColor Red
    exit 1
}
Write-Host "Push succeeded." -ForegroundColor Green

if ($PushOnly) {
    Write-Host "Push-only mode - done." -ForegroundColor Green
    exit 0
}

# Step 2: Build VM command
$cmds = "set -e; cd $VM_SRC && git pull"

$qecBuild  = @($Services | Where-Object { $_ -in @("qe-central","qe-explorer") })
$mainBuild = @($Services | Where-Object { $_ -eq "platform-api" })

if ($qecBuild.Count -gt 0) {
    $svcList = $qecBuild -join " "
    $cmds += "; cd $VM_SRC/Nexus_power"
    $cmds += "; docker compose -f $QEC_COMPOSE build $svcList"
    $cmds += "; docker compose -f $QEC_COMPOSE up -d --force-recreate $svcList"
}

if ($mainBuild.Count -gt 0) {
    $svcList = $mainBuild -join " "
    $cmds += "; cd $VM_SRC/Nexus_power"
    $cmds += "; docker compose -f $MAIN_COMPOSE build $svcList"
    $cmds += "; docker compose -f $MAIN_COMPOSE up -d --force-recreate $svcList"
}

$cmds += "; echo ''; echo 'Container status:'; docker ps --format 'table {{.Names}}\t{{.Status}}' | head -15"

# Step 3: SSH and deploy
Write-Host "`n[2/3] SSHing to $VM_NAME and deploying..." -ForegroundColor Blue

# Same stderr trap as the push: git-pull progress and docker build output both go
# to stderr, and under 'Stop' PowerShell 5.1 kills a deploy that is working.
$ErrorActionPreference = "Continue"
& gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$cmds"
$sshExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($sshExit -ne 0) {
    Write-Host "VM deploy failed." -ForegroundColor Red
    exit 1
}

Write-Host "`n[3/3] Deploy complete! Services restarted: $($Services -join ', ')" -ForegroundColor Green
Write-Host "Portal: https://136.85.106.73" -ForegroundColor Green
