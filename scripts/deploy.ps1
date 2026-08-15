# NexusQA one-command deploy
# Usage:
#   .\scripts\deploy.ps1                        # deploy all 3 services
#   .\scripts\deploy.ps1 qe-central             # deploy only qe-central
#   .\scripts\deploy.ps1 platform-api           # deploy only platform-api
#   .\scripts\deploy.ps1 -PushOnly              # just push, don't deploy to VM

param(
    [switch]$PushOnly,
    [switch]$RebuildBase,
    # A2 / Track 0.2 - the golden crawl gate is the INTEGRATION CONTRACT: no
    # build reaches the fleet without a green live crawl, hotfixes included.
    # Kept as an explicit opt-OUT rather than a flag you must remember, because
    # the deploy you skip the gate on is always the urgent one.
    [switch]$NoGate,
    # Retained so existing invocations keep working; the gate now runs anyway.
    [switch]$Gate,
    # NAMED ONLY. Adding this as a plain [string] made it the first POSITIONAL
    # parameter, so `deploy.ps1 qe-central qe-explorer` bound 'qe-central' to it
    # and deployed only ONE service - silently, with the banner cheerfully
    # reporting the reduced list. Explicit Position on $Services below is what
    # keeps positional binding where it has always been.
    [Parameter(Mandatory = $false)]
    [string]$GoldenAppId = "",
    [Parameter(Position = 0, ValueFromRemainingArguments)]
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
# Capture the pre-pull SHA so we can tell whether nexus-base:dev needs a rebuild:
# qe-central is built FROM nexus-base:dev, so a change under sdk/nexus-sdk or the
# base Dockerfile does NOT reach it until the base image itself is rebuilt.
$cmds = "set -e; cd $VM_SRC; " + 'NX_BEFORE=$(git rev-parse HEAD); git pull; NX_AFTER=$(git rev-parse HEAD)'

$qecBuild  = @($Services | Where-Object { $_ -in @("qe-central","qe-explorer") })
$mainBuild = @($Services | Where-Object { $_ -eq "platform-api" })

# Rebuild the base image before qe-central when the SDK / base Dockerfile changed
# in the pulled range (or -RebuildBase forced it). Guarded so a normal deploy
# never pays for it; qe-explorer uses its own Playwright image, not the base.
if ($qecBuild -contains "qe-central") {
    if ($RebuildBase) {
        $baseGuard = "true"
    } else {
        $baseGuard = "git -C $VM_SRC diff --name-only " + '$NX_BEFORE $NX_AFTER | grep -qE ''^Nexus_power/(sdk/nexus-sdk/|infrastructure/docker/Dockerfile\.base$)'''
    }
    $cmds += "; cd $VM_SRC/Nexus_power"
    $cmds += "; if $baseGuard; then echo '>> nexus-base:dev: SDK/base changed - rebuilding'; docker build -f infrastructure/docker/Dockerfile.base -t nexus-base:dev . ; else echo '>> nexus-base:dev: no rebuild needed'; fi"
}

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

function Invoke-GateRollback {
    param([string]$Reason)
    Write-Host "`nROLLING BACK - the fleet must not stay on a build the gate refused ($Reason)." -ForegroundColor Red
    $prev = "cat $VM_SRC/.last_green_deploy 2>/dev/null || true"
    $green = (& gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$prev" 2>$null | Select-Object -Last 1)
    $green = "$green".Trim()
    if (-not $green) {
        Write-Host "NO LAST-GREEN COMMIT RECORDED - cannot roll back automatically." -ForegroundColor Red
        Write-Host "The fleet is running an UNVERIFIED build. Roll back by hand." -ForegroundColor Red
        return
    }
    Write-Host "Restoring last green commit $green ..." -ForegroundColor Yellow
    # Rebuild from whichever compose file actually owns each service. Trying
    # only one silently left a main-compose service on the rejected build - a
    # rollback that reports success while the bad container keeps serving is
    # worse than no rollback, because it ends the investigation.
    $rb = "set -e; cd $VM_SRC; git checkout $green; cd Nexus_power; ok=0; " +
          "for f in $QEC_COMPOSE $MAIN_COMPOSE; do " +
          'for s in ' + "$svcList" + '; do ' +
          'if docker compose -f $f config --services 2>/dev/null | grep -qx "$s"; then ' +
          'docker compose -f $f build "$s" && docker compose -f $f up -d --force-recreate "$s" && ok=1; ' +
          'fi; done; done; test "$ok" = 1'
    & gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$rb"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Fleet restored to $green. The rejected build is NOT serving." -ForegroundColor Green
    } else {
        Write-Host "ROLLBACK FAILED - the fleet may be on a rejected build. Intervene now." -ForegroundColor Red
    }
}

# -- A2: prove the deploy with one real crawl --------------------------------
# A green test suite says the code does what its author believed; only a real
# crawl says the funnel still works. On 2026-08-14 seven deploys shipped and
# THREE broke something visible only in a live crawl - each found by a human
# 35 minutes at a time. Opt-in for now (-Gate) so it can be trialled without
# blocking anyone; make it unconditional once it has run green a few times.
if ($NoGate) {
    Write-Host "`nGATE SKIPPED (-NoGate). This build is on the fleet UNGATED." -ForegroundColor Yellow
    Write-Host "Program rule 1: run scripts/golden_crawl_gate.sh before trusting it." -ForegroundColor Yellow
}
elseif ($PushOnly) { }
else {
    if (-not $GoldenAppId) {
        $GoldenAppId = $env:NEXUS_GOLDEN_APP_ID
    }
    if (-not $GoldenAppId) {
        Write-Host "`nGate needs -GoldenAppId <app_id> (or `$env:NEXUS_GOLDEN_APP_ID)." -ForegroundColor Red
        Write-Host "Pass -NoGate to ship deliberately ungated." -ForegroundColor Red
        exit 1
    }
    Write-Host "`n[4/4] Golden crawl gate (this runs a REAL crawl; ~5-40 min)..." -ForegroundColor Blue
    $ErrorActionPreference = "Continue"
    # No backtick continuation here: a single trailing space after one turns the
    # whole block into a parse error, which is exactly how this shipped broken.
    $gateCmd = "cd $VM_SRC/Nexus_power && bash scripts/golden_crawl_gate.sh $GoldenAppId"
    & gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$gateCmd"
    $gateExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    # Exit 3 is the gate's VERDICT that the funnel regressed. Any other non-zero
    # code means it never reached a verdict - a dropped SSH connection, a crawl
    # that could not be dispatched, a run that timed out. Announcing those as a
    # regression is a false alarm, and a gate that cries wolf gets switched off.
    # THE GATE RUNS AFTER THE CONTAINER SWAP, so "blocks the deploy" can only
    # mean one thing honestly: the fleet does not STAY on a build the gate
    # refused. A red verdict returns the VM to the last commit that passed.
    # Without this, a red gate printed a warning while the bad build kept
    # serving - which is what "reports" rather than "blocks" actually meant.
    if ($gateExit -eq 3) {
        Write-Host "`nGOLDEN CRAWL GATE FAILED - the funnel regressed on this deploy." -ForegroundColor Red
        Invoke-GateRollback -Reason "funnel regression"
        exit 1
    }
    if ($gateExit -ne 0) {
        Write-Host "`nGOLDEN CRAWL GATE DID NOT COMPLETE (exit $gateExit)." -ForegroundColor Yellow
        Write-Host "This is NOT a regression verdict - the crawl or the connection failed." -ForegroundColor Yellow
        # An UNGATED build on the fleet is exactly what rule 1 forbids, and a
        # gate that could not reach a verdict has proven nothing about it. Live
        # example: a 409 single-flight lock let an ungated qe-central ship.
        Invoke-GateRollback -Reason "gate reached no verdict"
        exit 1
    }
    Write-Host "`nGolden crawl gate PASSED - no funnel regression." -ForegroundColor Green
    $markGreen = "cd $VM_SRC && git rev-parse HEAD > .last_green_deploy"
    & gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$markGreen" 2>&1 | Out-Null
    Write-Host "Recorded this commit as the last GREEN build." -ForegroundColor Green
}
