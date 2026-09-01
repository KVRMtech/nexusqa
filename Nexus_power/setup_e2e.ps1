#!/usr/bin/env pwsh
# ═══════════════════════════════════════════════════════════════
# Nexus QA — Full E2E Setup Script (Windows)
# ═══════════════════════════════════════════════════════════════
# This script installs all dependencies and prepares the environment
# for End-to-End production-like testing.
#
# Usage: .\setup_e2e.ps1
# ═══════════════════════════════════════════════════════════════

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Write-Step($step, $msg) {
    Write-Host ""
    Write-Host "  [$step] $msg" -ForegroundColor Cyan
    Write-Host "  $('─' * 50)" -ForegroundColor DarkGray
}

function Test-CommandExists($cmd) {
    $null = Get-Command $cmd -ErrorAction SilentlyContinue
    return $?
}

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║     Nexus QA — E2E Environment Setup             ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════════════════╝" -ForegroundColor Green

# ──────────────────────────────────────────────────
# Step 1: Install Git
# ──────────────────────────────────────────────────
Write-Step "1/10" "Checking Git..."
if (Test-CommandExists "git") {
    Write-Host "    ✓ Git already installed: $(git --version)" -ForegroundColor Green
} else {
    Write-Host "    Installing Git..." -ForegroundColor Yellow
    winget install --id Git.Git --accept-package-agreements --accept-source-agreements --silent
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    Write-Host "    ✓ Git installed" -ForegroundColor Green
}

# ──────────────────────────────────────────────────
# Step 2: Install Python 3.11
# ──────────────────────────────────────────────────
Write-Step "2/10" "Checking Python 3.11..."
$pyExe = $null
# Check common Python locations
$pyPaths = @(
    "C:\Users\harik\AppData\Local\Programs\Python\Python311\python.exe",
    "C:\Python311\python.exe",
    "C:\Program Files\Python311\python.exe"
)
foreach ($p in $pyPaths) {
    if (Test-Path $p) { $pyExe = $p; break }
}

if (-not $pyExe -and (Test-CommandExists "python")) {
    $ver = python --version 2>&1
    if ($ver -match "3\.11") {
        $pyExe = (Get-Command python).Source
    }
}

if ($pyExe) {
    Write-Host "    ✓ Python 3.11 found: $pyExe" -ForegroundColor Green
} else {
    Write-Host "    Installing Python 3.11..." -ForegroundColor Yellow
    winget install --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements --silent
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    # Find it
    foreach ($p in $pyPaths) {
        if (Test-Path $p) { $pyExe = $p; break }
    }
    if (-not $pyExe) { $pyExe = "python" }
    Write-Host "    ✓ Python 3.11 installed" -ForegroundColor Green
}

# ──────────────────────────────────────────────────
# Step 3: Install Node.js 20 LTS
# ──────────────────────────────────────────────────
Write-Step "3/10" "Checking Node.js..."
if (Test-CommandExists "node") {
    Write-Host "    ✓ Node.js already installed: $(node --version)" -ForegroundColor Green
} else {
    Write-Host "    Installing Node.js 20 LTS..." -ForegroundColor Yellow
    winget install --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements --silent
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    Write-Host "    ✓ Node.js installed" -ForegroundColor Green
}

# ──────────────────────────────────────────────────
# Step 4: Install Docker Desktop
# ──────────────────────────────────────────────────
Write-Step "4/10" "Checking Docker..."
if (Test-CommandExists "docker") {
    Write-Host "    ✓ Docker already installed: $(docker --version)" -ForegroundColor Green
} else {
    Write-Host "    Installing Docker Desktop..." -ForegroundColor Yellow
    Write-Host "    (This may require a system restart for WSL2)" -ForegroundColor DarkYellow
    winget install --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements --silent
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    Write-Host "    ✓ Docker Desktop installed (may need restart + manual launch)" -ForegroundColor Green
}

# ──────────────────────────────────────────────────
# Step 5: Refresh PATH and verify all tools
# ──────────────────────────────────────────────────
Write-Step "5/10" "Refreshing PATH & verifying installations..."
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Re-resolve python
if (-not $pyExe -or -not (Test-Path $pyExe -ErrorAction SilentlyContinue)) {
    foreach ($p in $pyPaths) {
        if (Test-Path $p) { $pyExe = $p; break }
    }
}

Write-Host "    Python : $pyExe"
Write-Host "    Node   : $(Get-Command node -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)"
Write-Host "    npm    : $(Get-Command npm -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)"
Write-Host "    Docker : $(Get-Command docker -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)"
Write-Host "    Git    : $(Get-Command git -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)"

# ──────────────────────────────────────────────────
# Step 6: Create .env from .env.example
# ──────────────────────────────────────────────────
Write-Step "6/10" "Setting up .env file..."
if (Test-Path ".env") {
    Write-Host "    ✓ .env already exists" -ForegroundColor Green
} elseif (Test-Path ".env.example") {
    Copy-Item ".env.example" ".env"
    # Set development-friendly values
    $envContent = Get-Content ".env" -Raw
    $envContent = $envContent -replace 'NEXUS_ENV=production', 'NEXUS_ENV=development'
    $envContent = $envContent -replace 'NEXUS_JWT_SECRET=change-this-to-a-random-secret', 'NEXUS_JWT_SECRET=dev-e2e-test-secret-do-not-use-in-production-32chars!'
    $envContent = $envContent -replace 'NEXUS_SECRET_KEY=change-this-to-a-random-64-char-string', 'NEXUS_SECRET_KEY=dev-e2e-nexus-secret-key-for-testing-only-do-not-use-in-prod-64'
    $envContent = $envContent -replace 'NEXUS_ADMIN_PASSWORD=change-this-password', 'NEXUS_ADMIN_PASSWORD=admin123'
    $envContent = $envContent -replace 'REDIS_PASSWORD=change-this-redis-password', 'REDIS_PASSWORD=nexus-redis-dev'
    $envContent = $envContent -replace 'NEO4J_PASSWORD=change-this-neo4j-password', 'NEO4J_PASSWORD=nexus-neo4j-dev'
    $envContent = $envContent -replace 'POSTGRES_PASSWORD=change-this-postgres-password', 'POSTGRES_PASSWORD=nexus-dev'
    $envContent = $envContent -replace 'NEXUS_DEVICE=cuda', 'NEXUS_DEVICE=cpu'
    Set-Content ".env" $envContent
    Write-Host "    ✓ .env created from .env.example (dev-friendly defaults)" -ForegroundColor Green
} else {
    Write-Host "    Creating minimal .env..." -ForegroundColor Yellow
    @"
NEXUS_ENV=development
NEXUS_PRODUCT_MODE=canonical
NEXUS_JWT_SECRET=dev-e2e-test-secret-do-not-use-in-production-32chars!
JWT_SECRET=dev-e2e-test-secret-do-not-use-in-production-32chars!
POSTGRES_USER=nexus
POSTGRES_PASSWORD=nexus-dev
POSTGRES_DB=nexus
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=nexus-redis-dev
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=nexus-neo4j-dev
OLLAMA_BASE_URL=http://localhost:11434
LLM_BACKEND=ollama
HEART_OLLAMA_MODEL=llama3.2:1b
BRAIN_OLLAMA_MODEL=llama3.2:1b
NEXUS_DEVICE=cpu
"@ | Set-Content ".env"
    Write-Host "    ✓ .env created with development defaults" -ForegroundColor Green
}

# ──────────────────────────────────────────────────
# Step 7: Create Python virtual environment & install deps
# ──────────────────────────────────────────────────
Write-Step "7/10" "Setting up Python virtual environment..."
if (Test-Path ".venv") {
    Write-Host "    ✓ .venv already exists" -ForegroundColor Green
} else {
    Write-Host "    Creating .venv..." -ForegroundColor Yellow
    & $pyExe -m venv .venv
    Write-Host "    ✓ .venv created" -ForegroundColor Green
}

# Activate venv
$venvPython = ".\.venv\Scripts\python.exe"
$venvPip = ".\.venv\Scripts\pip.exe"

Write-Host "    Upgrading pip..." -ForegroundColor Yellow
& $venvPython -m pip install --upgrade pip --quiet 2>&1 | Out-Null

Write-Host "    Installing core Python dependencies..." -ForegroundColor Yellow
# Install SDK first (base dependency for all engines)
if (Test-Path "sdk\nexus-sdk") {
    & $venvPip install -e "sdk\nexus-sdk" --quiet 2>&1
    Write-Host "      ✓ nexus-sdk installed" -ForegroundColor Green
}

# Install all engine requirements
$reqFiles = @(
    "engines\shield-engine\requirements.txt",
    "engines\ears-engine\requirements.txt",
    "engines\eyes-engine\requirements.txt",
    "engines\heart-engine\requirements.txt",
    "engines\backbone-engine\requirements.txt",
    "engines\nerves-engine\requirements.txt",
    "engines\legs-engine\requirements.txt",
    "engines\hands-engine\requirements.txt",
    "engines\spine-engine\requirements.txt",
    "engines\mouth-engine\requirements.txt",
    "engines\brain-engine\requirements.txt",
    "platform\auth-service\requirements.txt",
    "platform\gateway\requirements.txt",
    "platform\api\requirements.txt",
    "products\qa-orchestrator\requirements.txt",
    "products\nexus-qa-orchestrator\requirements.txt",
    "demo-server\requirements.txt"
)

foreach ($req in $reqFiles) {
    if (Test-Path $req) {
        $name = Split-Path (Split-Path $req -Parent) -Leaf
        Write-Host "      Installing $name deps..." -ForegroundColor DarkGray
        & $venvPip install -r $req --quiet 2>&1 | Out-Null
    }
}

# Install testing dependencies
Write-Host "    Installing test dependencies..." -ForegroundColor Yellow
& $venvPip install pytest pytest-asyncio pytest-cov httpx aiohttp psycopg2-binary alembic asyncpg --quiet 2>&1

Write-Host "    ✓ All Python dependencies installed" -ForegroundColor Green

# ──────────────────────────────────────────────────
# Step 8: Install Node.js client dependencies
# ──────────────────────────────────────────────────
Write-Step "8/10" "Installing client (React) dependencies..."
if (Test-Path "client\node_modules") {
    Write-Host "    ✓ node_modules already exists" -ForegroundColor Green
} else {
    Push-Location "client"
    npm install 2>&1
    Pop-Location
    Write-Host "    ✓ Client dependencies installed" -ForegroundColor Green
}

# ──────────────────────────────────────────────────
# Step 9: Start Docker infrastructure
# ──────────────────────────────────────────────────
Write-Step "9/10" "Starting Docker infrastructure (dev stack)..."
if (-not (Test-CommandExists "docker")) {
    Write-Host "    ⚠ Docker not found in PATH." -ForegroundColor Yellow
    Write-Host "    Please start Docker Desktop manually and re-run this step." -ForegroundColor Yellow
    Write-Host "    Command: docker compose -f docker-compose.dev.yml up -d" -ForegroundColor DarkGray
} else {
    try {
        docker info *>$null
        Write-Host "    Docker daemon is running." -ForegroundColor Green
        Write-Host "    Starting infrastructure services (Redis, Postgres, Neo4j, Ollama)..." -ForegroundColor Yellow
        docker compose -f docker-compose.dev.yml up -d
        Write-Host "    ✓ Infrastructure services started" -ForegroundColor Green

        # Wait for services to be healthy
        Write-Host "    Waiting for services to become healthy..." -ForegroundColor Yellow
        Start-Sleep -Seconds 10
        docker compose -f docker-compose.dev.yml ps
    } catch {
        Write-Host "    ⚠ Docker daemon not running. Start Docker Desktop first." -ForegroundColor Yellow
    }
}

# ──────────────────────────────────────────────────
# Step 10: Run database migrations
# ──────────────────────────────────────────────────
Write-Step "10/10" "Running database migrations..."
if (Test-CommandExists "docker") {
    try {
        # Check if postgres is healthy
        $pgReady = docker exec nexus-postgres pg_isready -U nexus 2>&1
        if ($pgReady -match "accepting connections") {
            Write-Host "    PostgreSQL is ready. Running Alembic migrations..." -ForegroundColor Yellow
            $env:DATABASE_URL = "postgresql+asyncpg://nexus:nexus-dev@localhost:5432/nexus"
            & $venvPython -m alembic upgrade head 2>&1
            Write-Host "    ✓ Database migrations complete" -ForegroundColor Green
        } else {
            Write-Host "    ⚠ PostgreSQL not ready yet. Run manually later:" -ForegroundColor Yellow
            Write-Host "    .\.venv\Scripts\python.exe -m alembic upgrade head" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "    ⚠ Could not run migrations: $_" -ForegroundColor Yellow
    }
} else {
    Write-Host "    ⚠ Docker not available. Skipping migrations." -ForegroundColor Yellow
}

# ──────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────
Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║     Setup Complete!                               ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Cyan
Write-Host "  ┌─────────────────────────────────────────────────┐" -ForegroundColor DarkGray
Write-Host "  │ 1. Start Docker Desktop (if not running)        │" -ForegroundColor DarkGray
Write-Host "  │ 2. docker compose -f docker-compose.dev.yml up -d│" -ForegroundColor DarkGray
Write-Host "  │ 3. .\.venv\Scripts\activate                      │" -ForegroundColor DarkGray
Write-Host "  │ 4. python scripts\start_all.py  (all engines)   │" -ForegroundColor DarkGray
Write-Host "  │ 5. python test_e2e.py           (E2E tests)     │" -ForegroundColor DarkGray
Write-Host "  │                                                  │" -ForegroundColor DarkGray
Write-Host "  │ Or full Docker deployment:                       │" -ForegroundColor DarkGray
Write-Host "  │   .\nexus.ps1 up                                 │" -ForegroundColor DarkGray
Write-Host "  │   python test_e2e.py                             │" -ForegroundColor DarkGray
Write-Host "  └─────────────────────────────────────────────────┘" -ForegroundColor DarkGray
Write-Host ""
