#!/usr/bin/env pwsh
# ═══════════════════════════════════════════════════════════════
# Nexus QA — Post-Reboot E2E Setup
# ═══════════════════════════════════════════════════════════════
# Run this after rebooting to complete the E2E setup.
# Usage: .\post_reboot_setup.ps1
# ═══════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Write-Step($num, $msg) { Write-Host "`n=== [$num/7] $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }

# ── Step 1: Refresh PATH ─────────────────────────────────────
Write-Step 1 "Refreshing PATH"
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
# Ensure Docker CLI is on PATH
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $dockerBin = "C:\Program Files\Docker\Docker\resources\bin"
    if (Test-Path "$dockerBin\docker.exe") {
        $env:Path += ";$dockerBin"
        Write-Ok "Added Docker to PATH"
    } else {
        Write-Fail "Docker CLI not found. Install Docker Desktop first."
        exit 1
    }
}
Write-Ok "PATH refreshed"

# ── Step 2: Start Docker Desktop ─────────────────────────────
Write-Step 2 "Starting Docker Desktop"
$dockerProc = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if (-not $dockerProc) {
    $dockerDesktopExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerDesktopExe) {
        Start-Process $dockerDesktopExe
        Write-Ok "Docker Desktop launched"
    } else {
        Write-Fail "Docker Desktop not found at expected path."
        exit 1
    }
} else {
    Write-Ok "Docker Desktop already running"
}

# ── Step 3: Wait for Docker daemon ───────────────────────────
Write-Step 3 "Waiting for Docker daemon to be ready (up to 120s)"
$maxWait = 120
$waited = 0
while ($waited -lt $maxWait) {
    try {
        $result = docker info 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Docker daemon is ready"
            break
        }
    } catch {}
    Start-Sleep -Seconds 5
    $waited += 5
    Write-Host "  ... waiting ($waited/$maxWait seconds)" -ForegroundColor Gray
}
if ($waited -ge $maxWait) {
    Write-Fail "Docker daemon did not start within ${maxWait}s."
    Write-Host "  Check Docker Desktop for errors. You may need to:" -ForegroundColor Yellow
    Write-Host "    1. Enable VT-x/AMD-V in BIOS (restart into BIOS)" -ForegroundColor Yellow
    Write-Host "    2. Restart Docker Desktop manually" -ForegroundColor Yellow
    exit 1
}

# ── Step 4: Start infrastructure containers ──────────────────
Write-Step 4 "Starting infrastructure (Redis, PostgreSQL, Neo4j, Ollama)"
docker compose -f docker-compose.dev.yml up -d
if ($LASTEXITCODE -ne 0) {
    Write-Fail "docker compose failed"
    exit 1
}
Write-Ok "Infrastructure containers started"

# ── Step 5: Wait for PostgreSQL to be healthy ─────────────────
Write-Step 5 "Waiting for PostgreSQL to be healthy (up to 60s)"
$maxWait = 60
$waited = 0
while ($waited -lt $maxWait) {
    $health = docker inspect --format='{{.State.Health.Status}}' nexus-postgres 2>$null
    if ($health -eq "healthy") {
        Write-Ok "PostgreSQL is healthy"
        break
    }
    Start-Sleep -Seconds 3
    $waited += 3
    Write-Host "  ... PostgreSQL status: $health ($waited/$maxWait seconds)" -ForegroundColor Gray
}
if ($waited -ge $maxWait) {
    Write-Warn "PostgreSQL health check timed out, attempting migrations anyway..."
}

# ── Step 6: Run database migrations ──────────────────────────
Write-Step 6 "Running Alembic database migrations"
$env:DATABASE_URL = "postgresql+asyncpg://nexus:nexus-dev@localhost:5432/nexus"
& ".\.venv\Scripts\python.exe" -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Alembic migration failed"
    Write-Host "  Check that PostgreSQL is running and accessible." -ForegroundColor Yellow
    exit 1
}
Write-Ok "Database migrations complete"

# ── Step 7: Pull Ollama model ─────────────────────────────────
Write-Step 7 "Pulling Ollama model (llama3.2:1b)"
$ollamaReady = $false
$maxWait = 60
$waited = 0
while ($waited -lt $maxWait) {
    try {
        docker exec nexus-ollama ollama list 2>$null
        if ($LASTEXITCODE -eq 0) { $ollamaReady = $true; break }
    } catch {}
    Start-Sleep -Seconds 5
    $waited += 5
}
if ($ollamaReady) {
    docker exec nexus-ollama ollama pull llama3.2:1b
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Model llama3.2:1b pulled successfully"
    } else {
        Write-Warn "Model pull failed — you can retry: docker exec nexus-ollama ollama pull llama3.2:1b"
    }
} else {
    Write-Warn "Ollama not ready yet. Pull model later: docker exec nexus-ollama ollama pull llama3.2:1b"
}

# ── Summary ───────────────────────────────────────────────────
Write-Host "`n" -NoNewline
Write-Host "═══════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Nexus QA — Infrastructure Ready!" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  Infrastructure services:" -ForegroundColor White
Write-Host "    Redis:      localhost:6379"
Write-Host "    PostgreSQL: localhost:5432"
Write-Host "    Neo4j:      localhost:7474 (browser) / :7687 (bolt)"
Write-Host "    Ollama:     localhost:11434"
Write-Host ""
Write-Host "  Next steps to run E2E tests:" -ForegroundColor White
Write-Host "    1. Update .env hostnames for local dev:" -ForegroundColor Yellow
Write-Host "         REDIS_HOST=localhost" -ForegroundColor Gray
Write-Host "         POSTGRES_HOST=localhost" -ForegroundColor Gray
Write-Host "         NEO4J_URI=bolt://localhost:7687" -ForegroundColor Gray
Write-Host "         OLLAMA_BASE_URL=http://localhost:11434" -ForegroundColor Gray
Write-Host "         (and similar *_OLLAMA_BASE_URL vars)" -ForegroundColor Gray
Write-Host ""
Write-Host "    2. Start all backend services:" -ForegroundColor Yellow
Write-Host "         .\.venv\Scripts\python.exe scripts\start_all.py" -ForegroundColor Gray
Write-Host ""
Write-Host "    3. Run E2E tests (in new terminal):" -ForegroundColor Yellow
Write-Host "         .\.venv\Scripts\python.exe -m pytest test_e2e.py -v" -ForegroundColor Gray
Write-Host ""
Write-Host "    OR use full Docker stack:" -ForegroundColor Yellow
Write-Host "         docker compose up -d --build" -ForegroundColor Gray
Write-Host "         .\.venv\Scripts\python.exe -m pytest test_e2e.py -v" -ForegroundColor Gray
Write-Host ""
