#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Nexus QA — Docker One-Click Start/Stop/Status
# ═══════════════════════════════════════════════════════════════
#
#   ./nexus.sh up          Build & start all services
#   ./nexus.sh down        Stop all services
#   ./nexus.sh status      Show service health status
#   ./nexus.sh logs <svc>  Tail logs for a service
#   ./nexus.sh restart <svc>  Rebuild & restart one service
#   ./nexus.sh reset       Stop everything & wipe all data
#
# ═══════════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")"

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'

header() {
    echo ""
    echo -e "${CYAN}  ╔══════════════════════════════════════════════╗${NC}"
    printf "${CYAN}  ║  %-44s║${NC}\n" "$1"
    echo -e "${CYAN}  ╚══════════════════════════════════════════════╝${NC}"
    echo ""
}

ACTION="${1:-status}"
shift || true

case "$ACTION" in

    up)
        header "Nexus QA — Starting All Services"

        echo -e "  ${YELLOW}[1/4] Pulling infrastructure images...${NC}"
        docker compose pull redis postgres neo4j ollama 2>/dev/null || true

        echo -e "  ${YELLOW}[2/4] Building base image (SDK layer)...${NC}"
        docker compose build base-image

        echo -e "  ${YELLOW}[3/4] Building service images...${NC}"
        docker compose build --parallel

        echo -e "  ${YELLOW}[4/4] Starting all services...${NC}"
        docker compose up -d

        echo -e "  ${YELLOW}Waiting for services to become healthy...${NC}"
        sleep 15

        docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"

        echo ""
        echo -e "  ${GREEN}✓ Nexus QA is running!${NC}"
        echo "  ┌──────────────────────────────────────────┐"
        echo "  │  UI:        http://localhost:3000         │"
        echo "  │  Gateway:   http://localhost:8080         │"
        echo "  │  Postgres:  localhost:5432                │"
        echo "  │  Neo4j:     http://localhost:7474         │"
        echo "  │  Ollama:    http://localhost:11434        │"
        echo "  └──────────────────────────────────────────┘"
        echo ""
        echo -e "  Tip: Run ./nexus.sh logs <service> to tail logs"
        ;;

    down)
        header "Nexus QA — Stopping All Services"
        docker compose down
        echo -e "  ${GREEN}✓ All services stopped.${NC}"
        ;;

    status)
        header "Nexus QA — Service Status"
        docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
        ;;

    logs)
        if [ $# -eq 0 ]; then
            echo -e "  ${YELLOW}Following ALL service logs (Ctrl+C to stop)...${NC}"
            docker compose logs -f --tail=50
        else
            echo -e "  ${YELLOW}Following logs for: $*...${NC}"
            docker compose logs -f --tail=50 "$@"
        fi
        ;;

    restart)
        if [ $# -eq 0 ]; then
            echo -e "  ${RED}Usage: ./nexus.sh restart <service-name>${NC}"
            exit 1
        fi
        header "Rebuilding: $*"
        docker compose up -d --build "$@"
        echo -e "  ${GREEN}✓ Restarted: $*${NC}"
        ;;

    build)
        header "Nexus QA — Building All Images"
        docker compose build --parallel
        echo -e "  ${GREEN}✓ All images built.${NC}"
        ;;

    reset)
        header "Nexus QA — Full Reset (DATA WILL BE LOST)"
        read -rp "  Type YES to confirm data wipe: " confirm
        if [ "$confirm" = "YES" ]; then
            docker compose down -v --remove-orphans
            echo -e "  ${GREEN}✓ All services stopped and data wiped.${NC}"
        else
            echo -e "  ${YELLOW}Cancelled.${NC}"
        fi
        ;;

    pull-model)
        model="${1:-llama3.2:1b}"
        header "Pulling Ollama model: $model"
        docker compose exec ollama ollama pull "$model"
        echo -e "  ${GREEN}✓ Model $model ready.${NC}"
        ;;

    *)
        echo "Usage: ./nexus.sh {up|down|status|logs|restart|build|reset|pull-model}"
        exit 1
        ;;
esac
