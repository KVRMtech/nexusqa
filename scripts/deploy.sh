#!/usr/bin/env bash
set -euo pipefail

# ─── NexusQA one-command deploy ───
# Usage:
#   ./scripts/deploy.sh                     # deploy all changed services
#   ./scripts/deploy.sh qe-central          # deploy only qe-central
#   ./scripts/deploy.sh platform-api        # deploy only platform-api
#   ./scripts/deploy.sh qe-explorer         # deploy only qe-explorer
#   ./scripts/deploy.sh --push-only         # just push, don't deploy to VM

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VM_NAME="verdict-box"
VM_ZONE="asia-southeast1-a"
VM_PROJECT="project-8d85a07a-396c-40aa-9b6"
VM_USER="srika"
VM_SRC="/home/${VM_USER}/nexus-src"
REMOTE="mine"
BRANCH="develop"

QEC_COMPOSE="docker-compose.qec.yml"
MAIN_COMPOSE="docker-compose.yml"

# Services that live in docker-compose.qec.yml
QEC_SERVICES="qe-central qe-explorer"
# Services that live in docker-compose.yml
MAIN_SERVICES="platform-api"

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[0;34m%s\033[0m\n' "$*"; }

die() { red "FATAL: $*" >&2; exit 1; }

# ─── Parse arguments ───
PUSH_ONLY=false
REBUILD_BASE=false          # force a nexus-base:dev rebuild regardless of diff
REQUESTED_SERVICES=()
for arg in "$@"; do
    case "$arg" in
        --push-only) PUSH_ONLY=true ;;
        --rebuild-base) REBUILD_BASE=true ;;
        qe-central|qe-explorer|platform-api) REQUESTED_SERVICES+=("$arg") ;;
        *) die "Unknown argument: $arg. Valid: qe-central, qe-explorer, platform-api, --push-only, --rebuild-base" ;;
    esac
done

# ─── Detect which services have changes (if none specified) ───
detect_changed_services() {
    local changed=()
    local diff_files
    diff_files="$(git diff HEAD~1 --name-only 2>/dev/null || echo "")"

    if echo "$diff_files" | grep -q "engines/qe-explorer/"; then
        changed+=("qe-explorer")
    fi
    if echo "$diff_files" | grep -q "platform/qe-central/"; then
        changed+=("qe-central")
    fi
    if echo "$diff_files" | grep -q "platform/api/"; then
        changed+=("platform-api")
    fi

    if [ ${#changed[@]} -eq 0 ]; then
        changed=("qe-central" "qe-explorer" "platform-api")
        blue "Could not detect changes — deploying all 3 services"
    fi

    echo "${changed[@]}"
}

if [ ${#REQUESTED_SERVICES[@]} -eq 0 ]; then
    read -ra REQUESTED_SERVICES <<< "$(detect_changed_services)"
fi

blue "Services to deploy: ${REQUESTED_SERVICES[*]}"

# ─── Step 1: Push to snapshot repo ───
blue "[1/3] Pushing to ${REMOTE}/${BRANCH}..."
if git push "$REMOTE" "$BRANCH" 2>&1; then
    green "Push succeeded."
else
    die "Push failed. Check SSH key: ssh -T git@github.com"
fi

if $PUSH_ONLY; then
    green "Push-only mode — done."
    exit 0
fi

# ─── Step 2: Build VM deploy command ───
build_vm_commands() {
    # Capture the pre-pull SHA so we can tell what the pull actually changed and
    # decide whether nexus-base:dev needs a rebuild. qe-central/repo-intel are
    # built FROM nexus-base:dev; a change under sdk/nexus-sdk or the base
    # Dockerfile does NOT reach them until the base image itself is rebuilt.
    local cmds="set -e; cd ${VM_SRC}; NX_BEFORE=\$(git rev-parse HEAD); git pull; NX_AFTER=\$(git rev-parse HEAD)"

    local qec_build=()
    local main_build=()
    local build_qe_central=false

    for svc in "${REQUESTED_SERVICES[@]}"; do
        case "$svc" in
            qe-central|qe-explorer)
                qec_build+=("$svc")
                [ "$svc" = "qe-central" ] && build_qe_central=true
                ;;
            platform-api)
                main_build+=("$svc")
                ;;
        esac
    done

    # Rebuild nexus-base:dev before qe-central when the SDK / base Dockerfile
    # changed in the pulled range (or --rebuild-base forced it). Guarded so a
    # normal deploy never pays for it; qe-explorer has its own Playwright image
    # and does not need the base.
    if [ "$build_qe_central" = true ]; then
        local base_guard
        if $REBUILD_BASE; then
            base_guard="true"
        else
            base_guard="git -C ${VM_SRC} diff --name-only \$NX_BEFORE \$NX_AFTER | grep -qE '^Nexus_power/(sdk/nexus-sdk/|infrastructure/docker/Dockerfile\\.base$)'"
        fi
        cmds+="; cd ${VM_SRC}/Nexus_power"
        cmds+="; if ${base_guard}; then echo '>> nexus-base:dev: SDK/base changed — rebuilding'; docker build -f infrastructure/docker/Dockerfile.base -t nexus-base:dev . ; else echo '>> nexus-base:dev: no rebuild needed'; fi"
    fi

    if [ ${#qec_build[@]} -gt 0 ]; then
        local svc_list="${qec_build[*]}"
        cmds+="; cd ${VM_SRC}/Nexus_power"
        cmds+="; docker compose -f ${QEC_COMPOSE} build ${svc_list}"
        cmds+="; docker compose -f ${QEC_COMPOSE} up -d --force-recreate ${svc_list}"
    fi

    if [ ${#main_build[@]} -gt 0 ]; then
        local svc_list="${main_build[*]}"
        cmds+="; cd ${VM_SRC}/Nexus_power"
        cmds+="; docker compose -f ${MAIN_COMPOSE} build ${svc_list}"
        cmds+="; docker compose -f ${MAIN_COMPOSE} up -d --force-recreate ${svc_list}"
    fi

    cmds+="; echo ''; echo 'Container status:'; docker ps --format 'table {{.Names}}\t{{.Status}}' | head -15"
    echo "$cmds"
}

VM_CMD="$(build_vm_commands)"

# ─── Step 3: SSH to VM and deploy ───
blue "[2/3] SSHing to ${VM_NAME} and deploying..."
blue "Running: ${VM_CMD}"
echo ""

gcloud compute ssh "${VM_USER}@${VM_NAME}" \
    --zone="$VM_ZONE" \
    --project="$VM_PROJECT" \
    --command="$VM_CMD"

green ""
green "Deploy complete. Services restarted: ${REQUESTED_SERVICES[*]}"
green "Portal: https://136.85.106.73"
