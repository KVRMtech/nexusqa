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

# ── M0.5: THE DEPLOY MUST CARRY THE PRODUCTION ENVIRONMENT ──────────────────
# Every `docker compose` call below used to run with NO --env-file, so the VM
# inherited the compose DEFAULTS: `NEXUS_ENV: ${NEXUS_ENV:-development}`.
#
# That single unset variable disarmed the entire safety spine on the box that is
# actually serving clients:
#   * boot_validator.validate_boot_safety only REFUSES in staging/production, so
#     it warned and booted — on a dev KEK, with whatever secrets were around;
#   * auth.assert_signing_key_usable permits a known development JWT secret in a
#     development environment, which is exactly what the fleet claimed to be;
#   * prod_guard's dev-only onboarding bypass (fences.onboarding_test_bypass) is
#     honoured in development and never in production — so a real client app
#     could be waved past attestation by a flag in its own row.
#
# `.env.production` is what scripts/verdict_box_bootstrap.sh already GENERATES on
# the box (NEXUS_ENV=production + 256-bit JWT/explorer/DB secrets + gcp_kms). It
# was simply never passed to the deploys that followed the bootstrap. Passing it
# is what makes the gate real, and it is also now REQUIRED: M0.5 removed the
# shipped defaults for NEXUS_JWT_SECRET and QEC_EXPLORER_TOKEN, so compose will
# refuse to start without them rather than authenticate with a value from this
# repository.
$ENV_FILE = ".env.production"

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

# ── THE DEPLOYMENT INVENTORY — captured ONCE, never re-derived (T-GT-01) ────
# This list is the answer to "what did this deploy touch?", and it is frozen
# here, before a single container is built. Everything downstream — the build
# commands, the manifest written to the VM, and the rollback — reads THIS.
#
# What it replaces: the build section assigned `$svcList` twice (once for the
# qec overlay, once for the main overlay) and `Invoke-GateRollback` read the
# same script-scoped variable afterwards. It therefore saw only whatever the
# LAST build block wrote — `platform-api` on a default 3-service deploy. A red
# gate rolled back one service in three, printed "Fleet restored to <sha>", and
# closed the incident while two containers kept serving the rejected build.
$DeployInventory = @($ValidServices | Where-Object { $_ -in $Services })
Write-Host "Deployment inventory (rollback set): $($DeployInventory -join ', ')" -ForegroundColor Cyan

# Step 1: Push
Write-Host "`n[1/4] Pushing to $REMOTE/$BRANCH..." -ForegroundColor Blue
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

# PRECONDITION, checked on the VM before anything is built: the production env
# file must exist. Failing here is the whole point — a deploy that silently
# falls back to compose defaults is a deploy that silently runs in development,
# which is the state this check exists to make impossible. The message names the
# script that creates the file, so the fix is one command away.
$cmds += "; cd $VM_SRC/Nexus_power"
$cmds += "; if [ ! -f $ENV_FILE ]; then " +
         "echo '>> FATAL: $ENV_FILE is missing on this box.'; " +
         "echo '>> Without it the fleet boots as NEXUS_ENV=development and the'; " +
         "echo '>> safety spine (boot gate, JWT secret gate, prod_guard bypass'; " +
         "echo '>> rule) is INERT. Generate it with:'; " +
         "echo '>>   NEXUS_KEK_GCP_KEY=... GCS_BACKUP_BUCKET=gs://... \\'; " +
         "echo '>>   bash scripts/verdict_box_bootstrap.sh'; " +
         "exit 1; fi"
# Prove what the fleet will actually run as — printed in the deploy log, so a
# regression to development is visible in the transcript, not only in a health
# check nobody reads.
$cmds += "; echo '>> deploy env file: $ENV_FILE'; grep -E '^NEXUS_ENV=' $ENV_FILE"
$cmds += "; grep -qE '^NEXUS_ENV=(staging|production)`$' $ENV_FILE || " +
         "{ echo '>> FATAL: $ENV_FILE does not set NEXUS_ENV to staging or production'; exit 1; }"

# Derived from the FROZEN inventory, not from $Services, so a later edit to
# $Services can no longer desynchronise what is built from what is rolled back.
$qecBuild  = @($DeployInventory | Where-Object { $_ -in @("qe-central","qe-explorer") })
$mainBuild = @($DeployInventory | Where-Object { $_ -eq "platform-api" })

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

# Distinct variable per overlay. They used to share one name, which is the whole
# of T-GT-01: the rollback read the survivor of that collision.
if ($qecBuild.Count -gt 0) {
    $qecSvcList = $qecBuild -join " "
    $cmds += "; cd $VM_SRC/Nexus_power"
    $cmds += "; docker compose --env-file $ENV_FILE -f $QEC_COMPOSE build $qecSvcList"
    $cmds += "; docker compose --env-file $ENV_FILE -f $QEC_COMPOSE up -d --force-recreate $qecSvcList"
}

if ($mainBuild.Count -gt 0) {
    $mainSvcList = $mainBuild -join " "
    $cmds += "; cd $VM_SRC/Nexus_power"
    $cmds += "; docker compose --env-file $ENV_FILE -f $MAIN_COMPOSE build $mainSvcList"
    $cmds += "; docker compose --env-file $ENV_FILE -f $MAIN_COMPOSE up -d --force-recreate $mainSvcList"
}

# ── Write the deployment manifest ON THE VM, from the frozen inventory ──────
# The manifest is the rollback's ONLY input. Written after the swap succeeds and
# before the gate runs, so the file on disk always describes the build that is
# actually serving. gate_manifest.py validates the service set, so an unknown
# service fails here — loudly, at deploy time — rather than at rollback time,
# when a bad inventory means restoring the wrong containers during an incident.
$manifestArgs = ($DeployInventory -join " ")
$cmds += "; cd $VM_SRC/Nexus_power"
$cmds += "; python3 scripts/gate_manifest.py build --out $VM_SRC/.deploy_manifest.json" +
         " --commit `$(git -C $VM_SRC rev-parse HEAD)" +
         " --deployed-at `$(date -u +%Y-%m-%dT%H:%M:%SZ) $manifestArgs"
$cmds += "; echo '>> deployment manifest:'; cat $VM_SRC/.deploy_manifest.json"

$cmds += "; echo ''; echo 'Container status:'; docker ps --format 'table {{.Names}}\t{{.Status}}' | head -15"

# ── PRE-SWAP HOST HEALTH PREFLIGHT (T-GT-05) ───────────────────────────────
# The rollback matrix says infrastructure failure must not revert a healthy
# deployment. The cleanest way to honour that is to notice the infrastructure
# failure BEFORE the fleet changes: at this point the previous build is still
# serving, so aborting costs nothing and reverts nothing. Post-swap the same
# check still runs inside the gate, where it aborts without rolling back - but
# by then something has already changed, which is why this preflight exists.
Write-Host "`n[2/4] Host health preflight (pre-swap)..." -ForegroundColor Blue
$ErrorActionPreference = "Continue"
$healthCmd = "bash $VM_SRC/Nexus_power/scripts/host_health.sh"
& gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$healthCmd"
$healthExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($healthExit -eq 4) {
    Write-Host "`nDEPLOY ABORTED - the host cannot support a trustworthy deploy." -ForegroundColor Red
    Write-Host "NOTHING WAS SWAPPED. The previous build is still serving, untouched." -ForegroundColor Green
    Write-Host "Fix the host (disk / pruned containers / dead services) and re-run." -ForegroundColor Yellow
    exit 2
}
if ($healthExit -ne 0) {
    # The preflight itself could not run (SSH, missing script on an older VM
    # checkout). That is not evidence of an unhealthy host, and it must not
    # silently become a green light either - say so and continue, because the
    # gate will check health again after the swap.
    Write-Host "Preflight INCONCLUSIVE (exit $healthExit) - continuing; the gate re-checks." -ForegroundColor Yellow
}

# Step 3: SSH and deploy
Write-Host "`n[3/4] SSHing to $VM_NAME and deploying..." -ForegroundColor Blue

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

Write-Host "`n[4/4] Swap complete. Services restarted: $($DeployInventory -join ', ')" -ForegroundColor Green
Write-Host "Portal: https://136.85.106.73" -ForegroundColor Green

function Invoke-GateRollback {
    param([string]$Reason)
    Write-Host "`nROLLING BACK - the fleet must not stay on a build the gate refused ($Reason)." -ForegroundColor Red
    Write-Host "Rollback set (from the deployment manifest): $($DeployInventory -join ', ')" -ForegroundColor Yellow
    # ONE rollback implementation, on the VM, driven by the manifest this deploy
    # wrote. deploy.ps1 no longer inlines a shell loop over a variable it hopes
    # is still correct, and gate_rollback_drill.sh no longer re-types a copy of
    # that loop: the drill and the incident run the SAME code, so the drill
    # actually proves the thing that will execute at 3am.
    $rb = "bash $VM_SRC/Nexus_power/scripts/gate_rollback.sh --src $VM_SRC --manifest $VM_SRC/.deploy_manifest.json"
    & gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$rb"
    $rbExit = $LASTEXITCODE
    if ($rbExit -eq 0) {
        Write-Host "Fleet restored. EVERY deployed service is off the rejected build." -ForegroundColor Green
    } elseif ($rbExit -eq 2) {
        Write-Host "ROLLBACK NOT ATTEMPTED - no green anchor or no usable manifest." -ForegroundColor Red
        Write-Host "The fleet is running an UNVERIFIED build. Roll back by hand." -ForegroundColor Red
    } else {
        Write-Host "ROLLBACK INCOMPLETE - the fleet is MIXED. Intervene now." -ForegroundColor Red
        Write-Host "Read the 'failed:' line above: those services are still on the rejected build." -ForegroundColor Red
    }
    return $rbExit
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
    # Tee the transcript: the gate's last line carries a machine-readable verdict,
    # which is the only thing that survives an SSH drop that eats the exit code.
    $gateOut = (& gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$gateCmd" 2>&1 | Tee-Object -Variable _t | Out-String)
    $gateExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"

    # ── THE ROLLBACK DECISION MATRIX (T-GT-05) ─────────────────────────────
    # Rollback follows DEPLOYMENT CORRECTNESS, never monitoring availability.
    #
    #   condition                     verdict            rollback
    #   ---------------------------   ----------------   --------
    #   funnel regressed              REGRESSION (3)     YES
    #   app could not produce a crawl APP_UNHEALTHY (1)  YES
    #   host unhealthy / DB unread    HOST_UNAVAIL (4)   NO  - abort
    #   SSH dropped, no verdict line  <transport>        NO  - abort
    #   gate passed                   PASS (0)           NO  - finalize
    #
    # What changed and why: host-health failure used to exit 1, which landed in
    # the catch-all "gate reached no verdict" branch and rolled back. So a full
    # disk on the VM reverted a deployment nothing had found fault with. An
    # infrastructure failure means we know LESS about the build, not that the
    # build is bad; reverting on it is an outage we inflict on ourselves. The
    # honest response is to abort, leave the fleet alone, say the build is
    # UNVERIFIED, and demand a re-run.
    $gateVerdict = ""
    if ($gateOut -match 'GATE_VERDICT=(\w+)') { $gateVerdict = $Matches[1] }

    if ($gateExit -eq 0 -and $gateVerdict -eq "PASS") {
        # fall through to finalize
    }
    elseif ($gateVerdict -eq "REGRESSION" -or $gateExit -eq 3) {
        Write-Host "`nGOLDEN CRAWL GATE FAILED - the funnel regressed on this deploy." -ForegroundColor Red
        Invoke-GateRollback -Reason "funnel regression" | Out-Null
        exit 1
    }
    elseif ($gateVerdict -eq "APP_UNHEALTHY" -or $gateExit -eq 1) {
        # The gate confirmed the host was healthy and the containers were up, and
        # the application STILL could not produce a crawl. That is a statement
        # about this build. Live example: a 409 single-flight lock let an ungated
        # qe-central ship.
        Write-Host "`nGOLDEN CRAWL GATE: the deployed build could not produce a crawl." -ForegroundColor Red
        Invoke-GateRollback -Reason "deployed application unhealthy" | Out-Null
        exit 1
    }
    elseif ($gateVerdict -eq "HOST_UNAVAILABLE" -or $gateExit -eq 4) {
        Write-Host "`nGATE ABORTED - the HOST is unhealthy. NO verdict on this build." -ForegroundColor Yellow
        Write-Host "NOT rolling back: infrastructure failure is not deployment failure." -ForegroundColor Yellow
        Write-Host "The fleet stays on this build, and this build is UNVERIFIED." -ForegroundColor Yellow
        Write-Host "Fix the host, then: bash scripts/golden_crawl_gate.sh $GoldenAppId" -ForegroundColor Yellow
        exit 2
    }
    else {
        # No verdict line at all: the transport failed, or the gate died before
        # it could speak. Same class as monitoring being unavailable - we learned
        # nothing about the build, so we change nothing about the fleet.
        Write-Host "`nGATE UNREACHABLE (exit $gateExit, verdict '$gateVerdict')." -ForegroundColor Yellow
        Write-Host "The gate never reported a verdict - treat as a transport/monitoring failure." -ForegroundColor Yellow
        Write-Host "NOT rolling back. The fleet stays on this build, and it is UNVERIFIED." -ForegroundColor Yellow
        Write-Host "Re-run the gate before trusting it: bash scripts/golden_crawl_gate.sh $GoldenAppId" -ForegroundColor Yellow
        exit 2
    }
    Write-Host "`nGolden crawl gate PASSED - no funnel regression." -ForegroundColor Green
    $markGreen = "cd $VM_SRC && git rev-parse HEAD > .last_green_deploy"
    & gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$markGreen" 2>&1 | Out-Null
    Write-Host "Recorded this commit as the last GREEN build." -ForegroundColor Green
}
