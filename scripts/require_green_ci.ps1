# NexusQA -- REFUSE A DEPLOY THAT CI HAS NOT PASSED  (Team H / H1)
#
# Usage:
#   .\scripts\require_green_ci.ps1 -Sha <sha>            # verdict on one commit
#   .\scripts\require_green_ci.ps1 -Sha <sha> -Explain   # also print every run seen
#
# Exit codes -- EVERY non-zero code is a REFUSAL. There is no path through this
# script that says "I could not tell, carry on":
#
#   0  GREEN        every gating workflow concluded success on this exact sha
#   1  RED          a gating workflow concluded failure/cancelled/timed_out
#   2  NEVER RAN    a gating workflow has no run at all on this sha
#   3  PENDING      a gating workflow is still queued or in progress
#   4  UNKNOWABLE   gh missing, unauthenticated, or the API did not answer
#
#
# WHAT THIS CLOSES
# ================
# Until this script existed there was NO link between the repository that runs
# CI and the repository the fleet deploys from:
#
#     laptop  --git push-->  mine (nexus-power-snapshot)  --git pull-->  VM
#     laptop  --git push-->  origin (KVRMtech/nexusqa)    --> CI runs here
#
# Two remotes, and the deploying one has no CI. Measured on 2026-08-31, the
# trunk branch carried 826 commits of which 21 had a successful `Nexus QA CI`
# run -- 2.5%. Nothing anywhere asked the question this script asks.
#
# deploy.ps1 calls this BEFORE its push to `mine`, so a commit CI has not
# passed never reaches the deploy remote at all, let alone the fleet.
#
#
# WHY IT DOES NOT TRUST "gh run list SAID SUCCESS"
# ===============================================
# Four workflows fire on every push, and they are wildly different in cost:
#
#     Nexus QA CI                      ~30 min
#     Browser Test Harness (M0.2)      ~60 min
#     M0.5 Security Gate               ~45 s
#     A11 Attestation Certification    ~50 s
#
# `Nexus QA CI` also runs under `concurrency: cancel-in-progress: true`, so a
# push that lands while the previous one is still building CANCELS it. On the
# real history that is not a corner case -- of the last 100 runs on trunk, 53
# were cancelled, 18 failed and 21 succeeded.
#
# So on commit 36adb1f the honest picture was:
#
#     M0.5 Security Gate              success   (42s)
#     A11 Attestation Certification   success   (1m0s)
#     Nexus QA CI                     CANCELLED (1m31s)   <-- the real suite
#
# A gate written as `gh run list --commit <sha> | grep success` passes that
# commit. It would be a gate that reliably reports green on exactly the commits
# whose test suite never finished. This script therefore requires a verdict
# from EACH NAMED GATING WORKFLOW, and treats "cancelled" as red rather than as
# absent -- a suite that was killed has not passed.
#
#
# WHY "LATEST RUN WINS" AND NOT "ANY RUN EVER SUCCEEDED"
# =====================================================
# A sha is a fixed tree, so re-running a workflow on it re-tests identical code.
# "Any success ever" would therefore let a red commit be laundered green by
# re-running until the flake goes the other way. Branch protection itself uses
# the latest conclusion, and so does this. -Explain prints the full run history
# for each workflow, so a laundering attempt is visible in the transcript
# rather than hidden behind the verdict.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Sha,

    [string]$Repo = "KVRMtech/nexusqa",

    # The workflows whose verdict is load-bearing. Deliberately NOT all four:
    #
    #   IN  - "Nexus QA CI"        the suite: lint, compile, both services'
    #                              tests, the DB/tenant-isolation contract,
    #                              crawl-smoke, docker-build, integrity-proof.
    #   IN  - "M0.5 Security Gate" the security ship-stoppers (T-SEC-01..12);
    #                              its job already sits in branch protection's
    #                              required contexts.
    #
    #   OUT - "Browser Test Harness (M0.2)"  ~60 min and its own lane; making a
    #         one-hour job block every deploy is a decision to take on evidence
    #         (Closure Plan A17), not to inherit from this script.
    #   OUT - "A11 Attestation Certification"  not in the required-contexts set
    #         on the protected branch; adding it here would make this gate
    #         stricter than the merge gate, which is a claim nobody has made.
    #
    # Overridable so the set can be widened without editing the deploy path.
    [string[]]$GatingWorkflows = @("Nexus QA CI", "M0.5 Security Gate"),

    [switch]$Explain
)

$ErrorActionPreference = "Stop"

function Write-Verdict {
    param([string]$Text, [string]$Color)
    Write-Host $Text -ForegroundColor $Color
}

# -- resolve to a FULL 40-character sha --------------------------------------
# `gh run list --commit <short-sha>` returns an EMPTY LIST and exit 0. Not an
# error, not a warning -- it silently matches nothing. Measured on this repo:
#
#     gh run list --commit d5130e4                                   -> []
#     gh run list --commit d5130e4843ff7b5fbda4d303180ffe60cbe9d6c3  -> 3 runs
#
# In this script an empty list means REFUSE, so the failure is safe rather than
# dangerous -- but it would refuse every legitimate deploy while printing "this
# commit has NO CI run", which is a false statement about a commit that is
# fully green. Resolve first; refuse if it cannot be resolved at all.
if ($Sha -notmatch '^[0-9a-fA-F]{40}$') {
    $ErrorActionPreference = "Continue"
    $resolved = (& git rev-parse --verify "$Sha^{commit}")
    $rvExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($rvExit -ne 0 -or -not ("$resolved" -match '^[0-9a-fA-F]{40}$')) {
        Write-Host ""
        Write-Verdict "REFUSED (4) - '$Sha' does not resolve to a commit in this clone." "Red"
        Write-Verdict "A sha that cannot be adjudicated is not a sha that gets deployed." "Red"
        exit 4
    }
    $ShaShort = $Sha
    $Sha = "$resolved".Trim()
    Write-Host ""
    Write-Host "   (resolved $ShaShort -> $Sha)"
}

Write-Host ""
Write-Host "== CI GATE ==============================================="
Write-Host "   repo     : $Repo"
Write-Host "   commit   : $Sha"
Write-Host "   requires : $($GatingWorkflows -join ' + ') == success"
Write-Host ""

# -- precondition: gh must be present and able to read the repository --------
# Fail CLOSED. A missing tool is not evidence that the build is fine, and this
# is exactly the branch where a lazier gate would shrug and let the deploy
# through -- which is the same as having no gate on any machine where gh is not
# installed.
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    Write-Verdict "REFUSED (4) - gh is not on PATH, so no CI verdict can be read." "Red"
    Write-Verdict "This gate fails CLOSED: an unreadable gate is a refused deploy." "Red"
    Write-Host   "Install: https://cli.github.com/  then: gh auth login"
    exit 4
}

$ErrorActionPreference = "Continue"
$runsJson = & gh run list --repo $Repo --commit $Sha --limit 100 `
                --json workflowName,status,conclusion,databaseId,createdAt,url
$ghExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($ghExit -ne 0) {
    Write-Verdict "REFUSED (4) - 'gh run list' failed (exit $ghExit)." "Red"
    Write-Verdict "No verdict could be read, so this deploy does not proceed." "Red"
    Write-Host   "Check: gh auth status   and that $Repo is readable by this token."
    exit 4
}

# -- parse, and FORCE ENUMERATION -------------------------------------------
# Two PowerShell 5.1 traps live in these four lines, and the second one silently
# green-washed a red commit while this script was being written:
#
#   1. `ConvertFrom-Json` on an empty JSON array yields NOTHING, and
#      `@(nothing)` is a ONE-element array holding $null. So `$runs.Count -eq 0`
#      was FALSE for a commit with no runs at all.
#      Measured: @('[]' | ConvertFrom-Json).Count  ->  1
#
#   2. `ConvertFrom-Json` does NOT enumerate an array down the pipeline in 5.1 --
#      it emits the whole Object[] as ONE pipeline item. So
#      `... | ConvertFrom-Json | Where-Object {...}` filtered nothing, $runs held
#      a single element that was itself the array of every run, and `$mine[-1]`
#      was that whole array rather than the latest run.
#
#      The consequence was not a crash. `$latest.conclusion` on an array returns
#      EVERY conclusion, and `@("success","success","cancelled") -eq "success"`
#      is PowerShell's filter operator, which returns the two matches -- a
#      non-empty, therefore TRUE, value. The gate reported GREEN on commit
#      36adb1f, whose `Nexus QA CI` run was CANCELLED. That is precisely the
#      green-wash this script exists to stop, reproduced inside the script
#      itself, and it was caught only because a known-red commit was used as a
#      control rather than trusting a run of known-green ones.
#
# Assigning to a variable BEFORE wrapping is what fixes both: `@($x)` flattens an
# existing Object[] to its real length, and the explicit $null test keeps the
# empty case genuinely empty.
$parsed = $null
try {
    $parsed = $runsJson | ConvertFrom-Json
} catch {
    Write-Verdict "REFUSED (4) - could not parse the gh response as JSON." "Red"
    exit 4
}
if ($null -eq $parsed) { $runs = @() } else { $runs = @($parsed) }

# -- the sha may not be on origin at all ------------------------------------
# The single most common real cause: the commit exists only on this laptop, or
# only on `mine`. It has therefore never been compiled by anything. Say so in
# those words -- "no runs found" invites the reader to assume a gh problem.
if ($runs.Count -eq 0) {
    Write-Verdict "REFUSED (2) - this commit has NO CI run of any kind." "Red"
    Write-Host   ""
    Write-Host   "Nothing has ever built or tested $Sha."
    Write-Host   "The usual cause is that it was never pushed to origin:"
    Write-Host   "    git push origin-https <branch>"
    Write-Host   "Wait for the run to finish, then re-run the deploy."
    exit 2
}

if ($Explain) {
    Write-Host "-- every run on this commit ($($runs.Count)):"
    foreach ($r in ($runs | Sort-Object createdAt)) {
        $c = $r.conclusion
        if ([string]::IsNullOrEmpty($c)) { $c = "(no conclusion yet)" }
        Write-Host ("     {0,-32} {1,-12} {2}" -f $r.workflowName, $r.status, $c)
    }
    Write-Host ""
}

# -- adjudicate each gating workflow on its LATEST run -----------------------
$refusals = @()
$worstExit = 0

foreach ($wf in $GatingWorkflows) {
    $mine = @($runs | Where-Object { $_.workflowName -eq $wf } | Sort-Object createdAt)

    if ($mine.Count -eq 0) {
        Write-Host ("   {0,-32} NEVER RAN" -f $wf) -ForegroundColor Red
        $refusals += "$wf has no run on this commit"
        if ($worstExit -lt 2) { $worstExit = 2 }
        continue
    }

    $latest = $mine[-1]

    if ($latest.status -ne "completed") {
        Write-Host ("   {0,-32} {1} (still running)" -f $wf, $latest.status) -ForegroundColor Yellow
        $refusals += "$wf is still $($latest.status) - no verdict yet"
        if ($worstExit -lt 3) { $worstExit = 3 }
        continue
    }

    if ($latest.conclusion -eq "success") {
        $extra = ""
        if ($mine.Count -gt 1) { $extra = "   (after $($mine.Count) runs on this sha - check why)" }
        Write-Host ("   {0,-32} success{1}" -f $wf, $extra) -ForegroundColor Green
        continue
    }

    # cancelled / failure / timed_out / action_required / startup_failure.
    # `cancelled` lands here ON PURPOSE: cancel-in-progress kills the suite
    # mid-flight, and a suite that was killed has not passed.
    Write-Host ("   {0,-32} {1}" -f $wf, $latest.conclusion.ToUpper()) -ForegroundColor Red
    Write-Host ("        {0}" -f $latest.url)
    $refusals += "$wf concluded $($latest.conclusion)"
    $worstExit = 1
}

Write-Host ""

if ($refusals.Count -eq 0) {
    Write-Verdict "CI GATE PASSED - every gating workflow is green on $Sha." "Green"
    exit 0
}

Write-Verdict "DEPLOY REFUSED - this commit has not passed CI." "Red"
foreach ($r in $refusals) { Write-Host "   * $r" -ForegroundColor Red }
Write-Host ""
Write-Host "NOTHING WAS PUSHED AND NOTHING WAS DEPLOYED. The fleet is untouched." -ForegroundColor Green
Write-Host "Fix the build, push, wait for green, then re-run the deploy." -ForegroundColor Yellow
exit $worstExit
