# NexusQA one-command deploy
# Usage:
#   .\scripts\deploy.ps1                        # deploy all 3 services
#   .\scripts\deploy.ps1 qe-central             # deploy only qe-central
#   .\scripts\deploy.ps1 platform-api           # deploy only platform-api
#   .\scripts\deploy.ps1 -PushOnly              # just push, don't deploy to VM
#
# Exit codes:
#   0  deployed and gated green
#   1  a step failed (push, VM deploy, or the golden crawl gate rolled back)
#   2  UNVERIFIED - the fleet carries this build but no gate reached a verdict
#   3  REFUSED BY THE CI GATE - nothing was pushed, nothing was deployed

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
# Overridable the same way scripts/gate0_require_ci_lanes.sh takes GATE0_BRANCH.
# It exists so the CI gate's refusal can be PROVEN against a named commit
# without moving `develop`, which nine concurrent sessions share -- and so a
# deliberate non-default-branch deploy is an explicit act rather than an edit to
# this file. Unset, it is exactly the previous behaviour.
$BRANCH      = if ($env:NEXUS_DEPLOY_BRANCH) { $env:NEXUS_DEPLOY_BRANCH } else { "develop" }
if ($env:NEXUS_DEPLOY_BRANCH) {
    Write-Host "NEXUS_DEPLOY_BRANCH is set - deploying '$BRANCH', NOT develop." -ForegroundColor Yellow
}

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

# -- H1: THE CI GATE -- NOTHING DEPLOYS WITHOUT A GREEN RUN ON ORIGIN --------
# This runs BEFORE the push, on purpose. The old order was
#
#     push to `mine`  ->  VM `git pull`  ->  build  ->  swap  ->  golden crawl
#
# and CI appeared nowhere in it. `mine` is the remote the VM pulls from and it
# has no Actions; `origin` is where CI runs and the deploy never asked it
# anything. Measured on 2026-08-31: 826 commits on trunk, 21 with a successful
# `Nexus QA CI` run. The golden crawl gate below is a good gate but it fires
# AFTER the fleet has already swapped -- it detects a bad build by serving it.
#
# Gating before the push means a commit CI has not passed never reaches the
# deploy remote at all, so there is nothing for the VM to pull even by accident.
# It also covers -PushOnly, which otherwise put an unverified sha one `git pull`
# away from the fleet.
#
# THERE IS DELIBERATELY NO BYPASS FLAG. `-NoGate` skips the golden CRAWL gate
# and always has; it does not skip this one. A switch that lets a red commit
# deploy is a switch that will be used on exactly the deploy that most needs the
# gate, which is the reasoning already written above `-NoGate`. If an override
# is ever wanted it should be added as a deliberate decision with its own
# record, not inherited from this line.
Write-Host "`n[0/4] CI gate - has origin already passed this commit?" -ForegroundColor Blue
Set-Location $PSScriptRoot\..

$ErrorActionPreference = "Continue"
$deployShaRaw = (& git rev-parse --verify "$BRANCH^{commit}")
$revExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($revExit -ne 0) {
    Write-Host "Cannot resolve '$BRANCH' to a commit. Nothing was pushed or deployed." -ForegroundColor Red
    exit 3
}
$DeploySha = "$deployShaRaw".Trim()

# Built with Join-Path, not string concatenation. This line shipped once as
# "$PSScriptRoot" + CR + "equire_green_ci.ps1": a single backslash before an
# `r` was read as an escape by the tool that wrote it, and a later text-mode
# read of this CRLF file then promoted that CR into a real line break. Both
# halves of that are CLAUDE.md section 3. Join-Path removes the backslash from
# the source entirely, so the class of accident cannot recur here.
$GateScript = Join-Path $PSScriptRoot "require_green_ci.ps1"

# FAIL CLOSED ON A MISSING GATE. While the path above was corrupted, PowerShell
# raised CommandNotFound -- and $LASTEXITCODE stayed 0. The deploy aborted on an
# unhandled exception while still reporting SUCCESS to whoever called it. A gate
# that cannot be found must REFUSE rather than evaporate: a check that is absent
# is indistinguishable from a check that passed, which is the single failure
# mode this whole script exists to remove.
if (-not (Test-Path $GateScript)) {
    Write-Host "DEPLOY REFUSED - the CI gate script is missing:" -ForegroundColor Red
    Write-Host "  $GateScript" -ForegroundColor Red
    Write-Host "A deploy does not proceed past a gate that is not there." -ForegroundColor Red
    exit 3
}

& $GateScript -Sha $DeploySha -Explain
$ciExit = $LASTEXITCODE
if ($ciExit -ne 0) {
    Write-Host "`nDEPLOY REFUSED BY THE CI GATE (require_green_ci exit $ciExit)." -ForegroundColor Red
    Write-Host "No push to '$REMOTE'. No pull on the VM. No build. No swap." -ForegroundColor Green
    Write-Host "The fleet is exactly as it was before this command ran." -ForegroundColor Green
    exit 3
}
Write-Host "CI gate passed for $DeploySha - proceeding to push." -ForegroundColor Green

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
    # Team A / Phase A: the egress proxy is a DEPLOY ARTIFACT too - its
    # squid.conf is bind-mounted from the repo and its entrypoint watcher lives
    # in compose, and the per-crawl fence (contracts/fleet_egress_fence_v1)
    # needs producer (qe-central) and consumer (squid) on the same protocol.
    # Recreate the proxy whenever qe-explorer deploys, so the consumer can
    # never lag the producer: an old squid.conf against the new writer denies
    # all egress (fail-closed but broken); recreating closes that window.
    # Deliberately NOT in the build list (stock image, nothing to build) and
    # NOT in the rollback manifest (a rollback checks out the old squid.conf,
    # and the next deploy's recreate applies it the same way).
    $qecUpList = $qecSvcList
    if ($qecBuild -contains "qe-explorer") { $qecUpList = "$qecSvcList qec-egress-proxy" }
    $cmds += "; cd $VM_SRC/Nexus_power"
    $cmds += "; docker compose --env-file $ENV_FILE -f $QEC_COMPOSE build $qecSvcList"
    $cmds += "; docker compose --env-file $ENV_FILE -f $QEC_COMPOSE up -d --force-recreate $qecUpList"
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
        # -- WAIT FOR THE FLEET TO BE READY, NOT MERELY RUNNING --------------
    #
    # MEASURED 2026-09-01, and it rolled back a GOOD build. The swap finished
    # at 23:08:49 with `nexus-qe-central  Up 4 seconds (health: starting)`; the
    # gate dispatched immediately, got no crawl, and reported APP_UNHEALTHY.
    # Twenty seconds later the fleet was reverted. One minute after that the
    # SAME image reported `Up About a minute (healthy)` - the build was fine
    # and had simply not finished starting.
    #
    # The false inference lives in golden_crawl_gate.sh: 'The containers were
    # confirmed running by host_health above, so a refused or failed dispatch
    # is the BUILD refusing work.' host_health tests
    # docker inspect {{.State.Running}} - RUNNING, not HEALTHY. A container is
    # Running the instant it starts and for the whole of its start_period,
    # during which it answers nothing. The premise holds everywhere EXCEPT the
    # seconds right after a swap, which is exactly when this gate runs.
    #
    # A gate that reverts a healthy deployment is worse than no gate: it
    # teaches everyone to pass -NoGate. Waiting is the whole fix.
    Write-Host "`nWaiting for the fleet to report HEALTHY (not merely running)..." -ForegroundColor Blue
    # Single-quoted so PowerShell interpolates nothing; '' is a literal quote.
    $readyCmd = 'for i in $(seq 1 60); do s=$(docker inspect -f ''{{if .State.Health}}{{.State.Health.Status}}{{else}}nohc{{end}}'' nexus-qe-central 2>/dev/null); case "$s" in healthy|nohc) echo READY; exit 0;; esac; sleep 10; done; echo NOT_READY; exit 1'
    $ErrorActionPreference = "Continue"
    & gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$readyCmd"
    $readyExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($readyExit -ne 0) {
        # NOT a rollback. A fleet that never became healthy is something we
        # cannot say about the BUILD from here - the same class as the host
        # being unavailable, which the matrix below already refuses to revert on.
        Write-Host "`nThe fleet did not report healthy within 10 minutes." -ForegroundColor Yellow
        Write-Host "NOT rolling back: never-became-ready is not proof the build is bad." -ForegroundColor Yellow
        Write-Host "This build is on the fleet and is UNVERIFIED. Investigate, then:" -ForegroundColor Yellow
        Write-Host "  bash scripts/golden_crawl_gate.sh $GoldenAppId" -ForegroundColor Yellow
        exit 2
    }

Write-Host "`n[4/4] Golden crawl gate (this runs a REAL crawl; ~5-40 min)..." -ForegroundColor Blue
    $ErrorActionPreference = "Continue"
    # No backtick continuation here: a single trailing space after one turns the
    # whole block into a parse error, which is exactly how this shipped broken.
    # RUN IT DETACHED AND POLL, rather than holding one pipe open for an hour.
    #
    # The gate legitimately takes ~45 minutes. Holding a single ssh session open
    # for that long makes the whole verification hostage to one TCP connection,
    # and on 2026-09-02 that connection died at minute 88 with "Network error:
    # Connection reset by peer". The crawl itself was running fine; the pipe was
    # not. Every long-lived-pipe failure looks like a build failure from here.
    #
    # setsid + nohup + </dev/null detaches the gate from this session entirely,
    # so it keeps running whatever happens to the operator's laptop, VPN or
    # wifi. The rc file is written by the SAME subshell that runs the gate, so
    # its existence is the completion signal and its content is the gate's own
    # exit code — never the ssh client's.
    #
    # Polls are short, cheap connections. A FAILED poll is just a retry: it can
    # no longer kill the run, which is the whole point of the change.
    #
    # NOTE the backtick in `$?: PowerShell escapes with a BACKTICK, not a
    # backslash. Written \$? this expands PowerShell's own $? (a boolean) and
    # ships `echo \True` to the box, so the rc file never holds a number, the
    # poll never matches, and every deploy hangs the full 90 minutes before
    # exiting 2. Caught by expanding the string before trusting it.
    $stamp    = Get-Date -Format "yyyyMMddHHmmss"
    $gateLog  = "/tmp/gate.$stamp.log"
    $gateRc   = "/tmp/gate.$stamp.rc"
    $startCmd = "cd $VM_SRC/Nexus_power && setsid nohup sh -c 'bash scripts/golden_crawl_gate.sh $GoldenAppId > $gateLog 2>&1; echo `$? > $gateRc' </dev/null >/dev/null 2>&1 & echo GATE_STARTED"
    $startOut = (& gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="$startCmd" 2>&1 | Out-String)
    if ($startOut -notmatch "GATE_STARTED") {
        Write-Host "`nCould not START the gate on the box:" -ForegroundColor Red
        Write-Host $startOut
        Write-Host "NOT rolling back: the gate never ran, so nothing is known about this build." -ForegroundColor Yellow
        exit 2
    }
    Write-Host "  gate running detached on the box (log: $gateLog)" -ForegroundColor DarkGray
    Write-Host "  polling every 30s; a dropped poll is retried, not fatal." -ForegroundColor DarkGray

    $gateExit   = $null
    $pollFails  = 0
    $maxMinutes = 90
    $deadline   = (Get-Date).AddMinutes($maxMinutes)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 30
        $probe = (& gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="cat $gateRc 2>/dev/null || echo RUNNING" 2>&1 | Out-String)
        # PARSE THE LAST NON-EMPTY LINE, not the whole blob. gcloud merges ssh
        # warnings ("Permanently added ... to the list of known hosts") into the
        # stream, so the probe is rarely just the number and an anchored match
        # against the whole string never fires — every deploy would then poll out
        # the full 90 minutes and report a healthy build as unverified.
        $probeLast = ($probe -split "`r?`n" | Where-Object { $_.Trim() -ne "" } | Select-Object -Last 1)
        if ($probeLast -match "^\s*(\d+)\s*$") { $gateExit = [int]$Matches[1]; break }
        if ($probeLast -match "RUNNING") { $pollFails = 0; continue }
        # Neither a number nor RUNNING: the probe itself failed. Tolerate a run
        # of these — the gate is unaffected by our inability to ask about it.
        $pollFails++
        Write-Host "  poll failed ($pollFails) - the gate is unaffected; retrying" -ForegroundColor DarkGray
        if ($pollFails -ge 20) {
            Write-Host "`n20 consecutive polls failed - cannot reach the box." -ForegroundColor Yellow
            Write-Host "NOT rolling back: this says nothing about the build. The gate may" -ForegroundColor Yellow
            Write-Host "still be running; read $gateLog on the box." -ForegroundColor Yellow
            exit 2
        }
    }
    if ($null -eq $gateExit) {
        Write-Host "`nGate did not finish within $maxMinutes minutes." -ForegroundColor Yellow
        Write-Host "NOT rolling back: a slow gate is not a failed build. Read $gateLog." -ForegroundColor Yellow
        exit 2
    }
    $gateOut = (& gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$VM_ZONE" --project="$VM_PROJECT" --command="cat $gateLog" 2>&1 | Out-String)
    $ErrorActionPreference = "Stop"

    # SHOW THE GATE ITS OWN VOICE. Tee-Object -Variable captures without echoing,
    # so for two consecutive deploys the gate printed exactly why it refused the
    # build and NOBODY EVER SAW IT - the console showed only this script's
    # one-line paraphrase, and diagnosing it meant SSHing in afterwards and
    # reading Postgres by hand, after the rollback had already destroyed the
    # containers holding the logs.
    #
    # A gate that cannot explain a refusal is not a gate, it is a coin toss with
    # extra steps. The transcript is printed unconditionally - a PASS is worth
    # reading too, because it is the only place the funnel numbers appear.
    Write-Host "`n--- golden crawl gate transcript -------------------------------"
    if ([string]::IsNullOrWhiteSpace($gateOut)) {
        Write-Host "  (the gate produced NO output - suspect the SSH transport, not the build)" -ForegroundColor Yellow
    } else {
        Write-Host $gateOut.TrimEnd()
    }
    Write-Host "--- end gate transcript (exit=$gateExit) -----------------------`n"

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

    # NO VERDICT LINE => NO ROLLBACK, WHATEVER THE EXIT CODE SAYS.
    #
    # The matrix below always said "SSH dropped, no verdict line -> NO, abort",
    # but the branches read `-or $gateExit -eq 1`, and the TRANSPORT shares exit
    # code 1 with the gate. Measured 2026-09-02: an 88-minute gate run ended
    # `FATAL ERROR: Network error: Connection reset by peer`, plink exited 1, and
    # a build nobody had found any fault with was rolled back for it.
    #
    # The gate's exit code is only meaningful when the gate SPOKE. Its verdict
    # line is printed by `finish` on every path it controls, so its absence means
    # the gate never reached a conclusion — which is the definition of learning
    # nothing about the build, and the fleet must not move on nothing.
    if (-not $gateVerdict) {
        Write-Host "`nGATE REACHED NO VERDICT - no GATE_VERDICT line in the transcript." -ForegroundColor Yellow
        Write-Host "That is a TRANSPORT failure (dropped SSH, killed session), not a" -ForegroundColor Yellow
        Write-Host "statement about this build. NOT rolling back: exit $gateExit from the" -ForegroundColor Yellow
        Write-Host "ssh client is not the gate's exit code." -ForegroundColor Yellow
        Write-Host "The fleet stays on this build and this build is UNVERIFIED. Re-run:" -ForegroundColor Yellow
        Write-Host "  bash scripts/golden_crawl_gate.sh $GoldenAppId" -ForegroundColor Yellow
        exit 2
    }

    if ($gateExit -eq 0 -and $gateVerdict -eq "PASS") {
        # fall through to finalize
    }
    elseif ($gateVerdict -eq "REGRESSION") {
        Write-Host "`nGOLDEN CRAWL GATE FAILED - the funnel regressed on this deploy." -ForegroundColor Red
        Invoke-GateRollback -Reason "funnel regression" | Out-Null
        exit 1
    }
    elseif ($gateVerdict -eq "APP_UNHEALTHY") {
        # The gate confirmed the host was healthy and the containers were up, and
        # the application STILL could not produce a crawl. That is a statement
        # about this build. Live example: a 409 single-flight lock let an ungated
        # qe-central ship.
        Write-Host "`nGOLDEN CRAWL GATE: the deployed build could not produce a crawl." -ForegroundColor Red
        Invoke-GateRollback -Reason "deployed application unhealthy" | Out-Null
        exit 1
    }
    elseif ($gateVerdict -eq "HOST_UNAVAILABLE") {
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
