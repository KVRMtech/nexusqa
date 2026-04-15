#!/usr/bin/env pwsh
# ═══════════════════════════════════════════════════════════════
# Nexus QA — Docker One-Click Start/Stop/Status
# ═══════════════════════════════════════════════════════════════
#
#   .\nexus.ps1 up          Build & start all services
#   .\nexus.ps1 down        Stop all services
#   .\nexus.ps1 status      Show service health status
#   .\nexus.ps1 logs <svc>  Tail logs for a service
#   .\nexus.ps1 restart <svc>  Rebuild & restart one service
#   .\nexus.ps1 reset       Stop everything & wipe all data
#
# ═══════════════════════════════════════════════════════════════

param(
    [Parameter(Position=0)]
    [ValidateSet("up","down","status","logs","restart","reset","build","pull-model")]
    [string]$Action = "status",

    [Parameter(Position=1, ValueFromRemainingArguments)]
    [string[]]$Services
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Write-Header($msg) {
    Write-Host ""
    Write-Host "  ╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "  ║  $($msg.PadRight(44))║" -ForegroundColor Cyan
    Write-Host "  ╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
}

function Show-Status {
    Write-Header "Nexus QA — Service Status"
    docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
}

switch ($Action) {

    "up" {
        Write-Header "Nexus QA — Starting All Services"

        Write-Host "  [1/4] Pulling infrastructure images..." -ForegroundColor Yellow
        docker compose pull redis postgres neo4j ollama 2>$null

        Write-Host "  [2/4] Building base image (SDK layer)..." -ForegroundColor Yellow
        docker compose build base-image

        Write-Host "  [3/4] Building service images..." -ForegroundColor Yellow
        docker compose build --parallel

        Write-Host "  [4/4] Starting all services..." -ForegroundColor Yellow
        docker compose up -d

        Write-Host ""
        Write-Host "  Waiting for services to become healthy..." -ForegroundColor Yellow
        Start-Sleep -Seconds 15

        Show-Status

        Write-Host ""
        Write-Host "  ✓ Nexus QA is running!" -ForegroundColor Green
        Write-Host "  ┌──────────────────────────────────────────┐" -ForegroundColor DarkGray
        Write-Host "  │  UI:        http://localhost:3000         │" -ForegroundColor DarkGray
        Write-Host "  │  Gateway:   http://localhost:8080         │" -ForegroundColor DarkGray
        Write-Host "  │  Postgres:  localhost:5432                │" -ForegroundColor DarkGray
        Write-Host "  │  Neo4j:     http://localhost:7474         │" -ForegroundColor DarkGray
        Write-Host "  │  Ollama:    http://localhost:11434        │" -ForegroundColor DarkGray
        Write-Host "  └──────────────────────────────────────────┘" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "  Tip: Run .\nexus.ps1 logs <service> to tail logs" -ForegroundColor DarkGray
    }

    "down" {
        Write-Header "Nexus QA — Stopping All Services"
        docker compose down
        Write-Host "  ✓ All services stopped." -ForegroundColor Green
    }

    "status" {
        Show-Status
    }

    "logs" {
        if (-not $Services) {
            Write-Host "  Following ALL service logs (Ctrl+C to stop)..." -ForegroundColor Yellow
            docker compose logs -f --tail=50
        } else {
            Write-Host "  Following logs for: $($Services -join ', ')..." -ForegroundColor Yellow
            docker compose logs -f --tail=50 @Services
        }
    }

    "restart" {
        if (-not $Services) {
            Write-Host "  Usage: .\nexus.ps1 restart <service-name>" -ForegroundColor Red
            Write-Host "  Example: .\nexus.ps1 restart spine" -ForegroundColor DarkGray
            exit 1
        }
        Write-Header "Rebuilding: $($Services -join ', ')"
        docker compose up -d --build @Services
        Write-Host "  ✓ Restarted: $($Services -join ', ')" -ForegroundColor Green
    }

    "build" {
        Write-Header "Nexus QA — Building All Images"
        docker compose build --parallel
        Write-Host "  ✓ All images built." -ForegroundColor Green
    }

    "reset" {
        Write-Header "Nexus QA — Full Reset (DATA WILL BE LOST)"
        $confirm = Read-Host "  Type YES to confirm data wipe"
        if ($confirm -eq "YES") {
            docker compose down -v --remove-orphans
            Write-Host "  ✓ All services stopped and data wiped." -ForegroundColor Green
        } else {
            Write-Host "  Cancelled." -ForegroundColor Yellow
        }
    }

    "pull-model" {
        $model = if ($Services) { $Services[0] } else { "llama3.2:1b" }
        Write-Header "Pulling Ollama model: $model"
        docker compose exec ollama ollama pull $model
        Write-Host "  ✓ Model $model ready." -ForegroundColor Green
    }
}
