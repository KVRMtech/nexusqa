# ─────────────────────────────────────────────────────────────
# Nexus QA Platform — Makefile
#
# Standardised build, test and deployment commands for CI/CD
# and local Linux/macOS development.
#
# Usage:
#   make help          Show all targets
#   make test          Run full test suite
#   make build         Build all Docker images
#   make push          Push images to registry
#
# NOTE: Windows users should use PowerShell directly or the
#       helper scripts in scripts/.
# ─────────────────────────────────────────────────────────────

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ─── Configurable variables ──────────────────────────────────
REGISTRY       ?= ghcr.io
IMAGE_OWNER    ?= nexus-qa
IMAGE_PREFIX   ?= $(REGISTRY)/$(IMAGE_OWNER)
TAG            ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
PYTHON         ?= python3
PYTEST_ARGS    ?= -v --tb=short

# ─── Service lists ───────────────────────────────────────────
CPU_ENGINES := shield-engine backbone-engine nerves-engine \
               legs-engine hands-engine spine-engine mouth-engine brain-engine
GPU_ENGINES := ears-engine eyes-engine heart-engine
ALL_ENGINES := $(CPU_ENGINES) $(GPU_ENGINES)
PLATFORMS   := auth-service gateway platform-api
ALL_SERVICES := $(ALL_ENGINES) $(PLATFORMS) orchestrator client

# ─────────────────────────────────────────────────────────────
# Help
# ─────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show all available targets
	@echo ""
	@echo "Nexus QA Platform — Make Targets"
	@echo "================================"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ─────────────────────────────────────────────────────────────
# Canonical processing rebuild (operator entry point)
# ─────────────────────────────────────────────────────────────
.PHONY: rebuild-canonical
rebuild-canonical: ## Rebuild + migrate + start canonical processing stack; exits 0 only when ready
	bash scripts/rebuild_canonical.sh

.PHONY: restart-canonical
restart-canonical: ## Restart the stack without rebuilding images (skips build + migrate)
	SKIP_BUILD=1 SKIP_MIGRATE=1 bash scripts/rebuild_canonical.sh

# ─────────────────────────────────────────────────────────────
# Development
# ─────────────────────────────────────────────────────────────
.PHONY: install
install: ## Install SDK and test dependencies into current virtualenv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e sdk/nexus-sdk/
	$(PYTHON) -m pip install pytest pytest-asyncio pytest-cov httpx faker jinja2

.PHONY: test
test: ## Run the full test suite (excluding e2e)
	$(PYTHON) -m pytest tests/ --ignore=tests/e2e $(PYTEST_ARGS)

.PHONY: test-cov
test-cov: ## Run tests with coverage reporting
	$(PYTHON) -m pytest tests/ --ignore=tests/e2e $(PYTEST_ARGS) \
		--cov=sdk --cov=engines --cov=platform --cov=products \
		--cov-report=term-missing --cov-report=xml:coverage.xml

.PHONY: lint
lint: ## Run Ruff linter and format check
	ruff check . --select E,W,F,I --ignore E501
	ruff format --check .

.PHONY: format
format: ## Auto-format Python code
	ruff format .
	ruff check . --select I --fix

.PHONY: compile-check
compile-check: ## Verify all .py files compile without errors
	@FAIL=0; \
	for f in $$(find . -name '*.py' \
	    -not -path './.venv/*' \
	    -not -path './alembic/*' \
	    -not -path './node_modules/*'); do \
		if ! $(PYTHON) -m py_compile "$$f" 2>/dev/null; then \
			echo "FAIL: $$f"; FAIL=1; \
		fi; \
	done; \
	if [ $$FAIL -ne 0 ]; then exit 1; fi; \
	echo "All files compile OK"

# ─────────────────────────────────────────────────────────────
# Frontend
# ─────────────────────────────────────────────────────────────
.PHONY: frontend-install
frontend-install: ## Install frontend dependencies
	cd client && npm ci

.PHONY: frontend-test
frontend-test: ## Run frontend tests
	cd client && npx vitest run

.PHONY: frontend-build
frontend-build: ## Build frontend for production
	cd client && npm run build

.PHONY: frontend-lint
frontend-lint: ## Lint frontend code
	cd client && npm run lint

# ─────────────────────────────────────────────────────────────
# Docker — Base Images
# ─────────────────────────────────────────────────────────────
.PHONY: build-base
build-base: ## Build shared base images (CPU + GPU)
	docker build \
		-f infrastructure/docker/Dockerfile.base \
		-t $(IMAGE_PREFIX)-base:$(TAG) \
		-t $(IMAGE_PREFIX)-base:latest .
	docker build \
		-f infrastructure/docker/Dockerfile.gpu \
		-t $(IMAGE_PREFIX)-gpu:$(TAG) \
		-t $(IMAGE_PREFIX)-gpu:latest .

# ─────────────────────────────────────────────────────────────
# Docker — Service Images
# ─────────────────────────────────────────────────────────────
.PHONY: build-engines
build-engines: build-base ## Build all engine images
	@for engine in $(CPU_ENGINES); do \
		echo "Building $$engine..."; \
		docker build \
			--build-arg BASE_IMAGE=$(IMAGE_PREFIX)-base:$(TAG) \
			-t $(IMAGE_PREFIX)-$$engine:$(TAG) \
			-t $(IMAGE_PREFIX)-$$engine:latest \
			engines/$$engine/; \
	done
	@for engine in $(GPU_ENGINES); do \
		echo "Building $$engine..."; \
		docker build \
			--build-arg BASE_IMAGE=$(IMAGE_PREFIX)-gpu:$(TAG) \
			-t $(IMAGE_PREFIX)-$$engine:$(TAG) \
			-t $(IMAGE_PREFIX)-$$engine:latest \
			engines/$$engine/; \
	done

.PHONY: build-platform
build-platform: build-base ## Build platform service images
	docker build \
		--build-arg BASE_IMAGE=$(IMAGE_PREFIX)-base:$(TAG) \
		-t $(IMAGE_PREFIX)-auth-service:$(TAG) \
		-t $(IMAGE_PREFIX)-auth-service:latest \
		platform/auth-service/
	docker build \
		--build-arg BASE_IMAGE=$(IMAGE_PREFIX)-base:$(TAG) \
		-t $(IMAGE_PREFIX)-gateway:$(TAG) \
		-t $(IMAGE_PREFIX)-gateway:latest \
		platform/gateway/
	docker build \
		--build-arg BASE_IMAGE=$(IMAGE_PREFIX)-base:$(TAG) \
		-t $(IMAGE_PREFIX)-platform-api:$(TAG) \
		-t $(IMAGE_PREFIX)-platform-api:latest \
		platform/api/

.PHONY: build-orchestrator
build-orchestrator: build-base ## Build orchestrator image
	docker build \
		--build-arg BASE_IMAGE=$(IMAGE_PREFIX)-base:$(TAG) \
		-t $(IMAGE_PREFIX)-orchestrator:$(TAG) \
		-t $(IMAGE_PREFIX)-orchestrator:latest \
		products/nexus-qa-orchestrator/

.PHONY: build-client
build-client: ## Build frontend client image
	docker build \
		-f infrastructure/docker/Dockerfile.client \
		-t $(IMAGE_PREFIX)-client:$(TAG) \
		-t $(IMAGE_PREFIX)-client:latest .

.PHONY: build
build: build-engines build-platform build-orchestrator build-client ## Build ALL Docker images

# ─────────────────────────────────────────────────────────────
# Docker — Push
# ─────────────────────────────────────────────────────────────
.PHONY: push
push: ## Push all images to container registry
	@for img in base gpu $(ALL_ENGINES) $(PLATFORMS) orchestrator client; do \
		echo "Pushing $(IMAGE_PREFIX)-$$img:$(TAG)..."; \
		docker push $(IMAGE_PREFIX)-$$img:$(TAG); \
		docker push $(IMAGE_PREFIX)-$$img:latest; \
	done

# ─────────────────────────────────────────────────────────────
# Local Infrastructure (Dev — infrastructure only)
# ─────────────────────────────────────────────────────────────
.PHONY: dev-up
dev-up: ## Start development infrastructure (Redis, Postgres, Neo4j, Ollama)
	docker compose -f docker-compose.dev.yml up -d

.PHONY: dev-down
dev-down: ## Stop development infrastructure
	docker compose -f docker-compose.dev.yml down

.PHONY: dev-logs
dev-logs: ## Tail logs from development infrastructure
	docker compose -f docker-compose.dev.yml logs -f

# ─────────────────────────────────────────────────────────────
# Production Stack (Full on-prem deployment)
# ─────────────────────────────────────────────────────────────
COMPOSE_PROD := infrastructure/docker/docker-compose.yml
COMPOSE_ENV  := infrastructure/docker/.env

.PHONY: prod-check-env
prod-check-env: ## Validate production .env exists with no default secrets
	@if [ ! -f $(COMPOSE_ENV) ]; then \
		echo "ERROR: $(COMPOSE_ENV) not found."; \
		echo "  cp infrastructure/docker/.env.example $(COMPOSE_ENV)"; \
		echo "  Then update all CHANGE-ME values."; \
		exit 1; \
	fi
	@if grep -q 'CHANGE-ME' $(COMPOSE_ENV) 2>/dev/null; then \
		echo "ERROR: $(COMPOSE_ENV) still contains CHANGE-ME placeholder secrets."; \
		echo "  Update all passwords/secrets before deploying."; \
		exit 1; \
	fi
	@echo "✓ .env file validated"

.PHONY: prod-up
prod-up: prod-check-env ## Start full production stack (requires pre-built images or will build)
	docker compose -f $(COMPOSE_PROD) --env-file $(COMPOSE_ENV) up -d

.PHONY: prod-build
prod-build: build ## Build all images then start production stack
	docker compose -f $(COMPOSE_PROD) --env-file $(COMPOSE_ENV) up -d --build

.PHONY: prod-down
prod-down: ## Stop production stack
	docker compose -f $(COMPOSE_PROD) --env-file $(COMPOSE_ENV) down

.PHONY: prod-logs
prod-logs: ## Tail logs from production stack
	docker compose -f $(COMPOSE_PROD) --env-file $(COMPOSE_ENV) logs -f

.PHONY: prod-ps
prod-ps: ## Show status of production stack services
	docker compose -f $(COMPOSE_PROD) --env-file $(COMPOSE_ENV) ps

.PHONY: prod-restart
prod-restart: ## Restart all production services
	docker compose -f $(COMPOSE_PROD) --env-file $(COMPOSE_ENV) restart

.PHONY: health
health: ## Check all service health endpoints
	$(PYTHON) scripts/health_check.py

# ─────────────────────────────────────────────────────────────
# Load Testing
# ─────────────────────────────────────────────────────────────
.PHONY: load-test
load-test: ## Run load test against running stack (default: smoke-test profile)
	$(PYTHON) tests/load/run_load_test.py --profile smoke-test --host http://localhost:8080

.PHONY: load-test-full
load-test-full: ## Run full load test with SLA gates
	$(PYTHON) tests/load/run_load_test.py --profile load-test --host http://localhost:8080

.PHONY: load-test-docker
load-test-docker: ## Start Locust web UI inside Docker stack (port 8089)
	docker compose -f $(COMPOSE_PROD) --profile load-test up -d locust
	@echo "Locust web UI: http://localhost:8089"

.PHONY: stress-test
stress-test: ## Run stress test to find breaking points
	$(PYTHON) tests/load/run_load_test.py --profile stress-test --host http://localhost:8080 --skip-sla

.PHONY: validate-secrets
validate-secrets: ## Validate all deployment secrets are properly configured
	$(PYTHON) scripts/validate_secrets.py --env docker --env-file $(COMPOSE_ENV)

# ─────────────────────────────────────────────────────────────
# Demo (Real E2E Production Demo)
# ─────────────────────────────────────────────────────────────
.PHONY: demo-setup
demo-setup: ## Pull Ollama models for real AI demo (one-time, ~6GB)
	$(PYTHON) scripts/setup_ollama.py

.PHONY: demo-quick
demo-quick: ## Run quick E2E demo (Shield + Heart, ~30s)
	$(PYTHON) scripts/demo_e2e.py --demo quick

.PHONY: demo
demo: ## Run full E2E production demo (all 10 engines, ~2-5min)
	$(PYTHON) scripts/demo_e2e.py --demo full

.PHONY: demo-orchestrator
demo-orchestrator: ## Run orchestrator chain demo (~3-10min)
	$(PYTHON) scripts/demo_e2e.py --demo orchestrator

# ─────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────
.PHONY: db-migrate
db-migrate: ## Run Alembic migrations to head
	cd alembic && $(PYTHON) -m alembic upgrade head

.PHONY: db-check
db-check: ## Verify database connectivity
	$(PYTHON) scripts/check_db.py

# ─────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Remove build artefacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
	rm -f coverage.xml
	rm -rf htmlcov/ dist/ build/

.PHONY: clean-docker
clean-docker: ## Remove all nexus Docker images
	@docker images --filter "reference=$(IMAGE_PREFIX)-*" -q | \
		xargs -r docker rmi -f 2>/dev/null || true
	@echo "Nexus images removed"
