# GitOps operator workflows. All targets are idempotent.
#
# Required environment variables:
#   REPO_URL              git repo URL where this directory lives
#   REPO_REVISION         branch / tag / commit pinned for bootstrap
#   CLUSTER_NAME          target cluster (used as Argo CD cluster secret name)
#   ENV                   one of: dev | staging | production
#
# Required tools on operator machine:
#   kubectl, helm, argocd CLI, envsubst, yq, jq

SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

# ── Inputs (override on the command line) ─────────────────────────────
REPO_URL              ?= $(error REPO_URL must be set, e.g. https://github.com/yourorg/nexus-power)
REPO_REVISION         ?= main
CLUSTER_NAME          ?= $(error CLUSTER_NAME must be set)
ENV                   ?= dev

# ── Derived paths ─────────────────────────────────────────────────────
HERE                  := $(abspath .)
BOOTSTRAP_DIR         := $(HERE)/bootstrap
PROJECTS_DIR          := $(HERE)/projects
APPLICATIONSETS_DIR   := $(HERE)/applicationsets
ENV_CONFIG            := $(HERE)/envs/$(ENV)/config.yaml
RENDER_DIR            := /tmp/gitops-render-$(CLUSTER_NAME)-$(ENV)

# Allowed values to guard against typos.
VALID_ENVS            := dev staging production

# ── Helpers ───────────────────────────────────────────────────────────
.PHONY: _validate-env
_validate-env:
	@if [[ ! " $(VALID_ENVS) " =~ " $(ENV) " ]]; then \
	  echo "ENV must be one of: $(VALID_ENVS)"; exit 1; \
	fi
	@if [[ ! -f "$(ENV_CONFIG)" ]]; then \
	  echo "Missing env config: $(ENV_CONFIG)"; exit 1; \
	fi

# Render bootstrap manifests with substitutions resolved. Output goes to
# RENDER_DIR so the operator can review before applying.
.PHONY: render
render: _validate-env
	@rm -rf "$(RENDER_DIR)" && mkdir -p "$(RENDER_DIR)"
	@PLATFORM_REPO_URL="$(REPO_URL)" \
	  PLATFORM_TARGET_REVISION="$$(yq -r .targetRevision "$(ENV_CONFIG)")" \
	  envsubst < "$(BOOTSTRAP_DIR)/00-namespaces.yaml"        > "$(RENDER_DIR)/00-namespaces.yaml"
	@PLATFORM_REPO_URL="$(REPO_URL)" \
	  PLATFORM_TARGET_REVISION="$$(yq -r .targetRevision "$(ENV_CONFIG)")" \
	  envsubst < "$(BOOTSTRAP_DIR)/10-argocd.yaml"            > "$(RENDER_DIR)/10-argocd.yaml"
	@PLATFORM_REPO_URL="$(REPO_URL)" \
	  PLATFORM_TARGET_REVISION="$$(yq -r .targetRevision "$(ENV_CONFIG)")" \
	  envsubst < "$(BOOTSTRAP_DIR)/20-argocd-projects.yaml"   > "$(RENDER_DIR)/20-argocd-projects.yaml"
	@PLATFORM_REPO_URL="$(REPO_URL)" \
	  PLATFORM_TARGET_REVISION="$$(yq -r .targetRevision "$(ENV_CONFIG)")" \
	  envsubst < "$(BOOTSTRAP_DIR)/30-root-app.yaml"          > "$(RENDER_DIR)/30-root-app.yaml"
	@echo "Rendered manifests under $(RENDER_DIR)"

# One-time per cluster. Installs Argo CD then hands over to GitOps.
# After this completes the operator should not need to touch this cluster
# again for routine changes — everything flows from Git.
.PHONY: bootstrap
bootstrap: render
	@echo "Bootstrapping $(CLUSTER_NAME) (env=$(ENV)) against $(REPO_URL)@$(REPO_REVISION)"
	# 1. Apply namespaces with PodSecurity labels.
	kubectl apply -f "$(RENDER_DIR)/00-namespaces.yaml"
	# 2. Install Argo CD via its upstream Helm chart. After this seed
	#    install, Argo CD self-manages via the Application in 10-argocd.yaml.
	helm repo add argo https://argoproj.github.io/argo-helm || true
	helm repo update
	helm upgrade --install argocd argo/argo-cd \
	  --namespace argocd \
	  --version 7.7.7 \
	  --wait \
	  --timeout 10m \
	  -f <(yq '.spec.source.helm.values' "$(RENDER_DIR)/10-argocd.yaml")
	# 3. Register the cluster with Argo CD (creates the cluster Secret
	#    that the ApplicationSet cluster generators select on).
	kubectl -n argocd label secret \
	  $$(kubectl -n argocd get secret -l argocd.argoproj.io/secret-type=cluster -o jsonpath='{.items[0].metadata.name}') \
	  env=$(ENV) \
	  --overwrite || true
	kubectl -n argocd annotate secret \
	  $$(kubectl -n argocd get secret -l argocd.argoproj.io/secret-type=cluster -o jsonpath='{.items[0].metadata.name}') \
	  platform-target-revision=$$(yq -r .targetRevision "$(ENV_CONFIG)") \
	  --overwrite || true
	# 4. Apply the bootstrap Applications: Argo CD takeover, projects, root.
	kubectl apply -f "$(RENDER_DIR)/10-argocd.yaml"
	kubectl apply -f "$(RENDER_DIR)/20-argocd-projects.yaml"
	kubectl apply -f "$(RENDER_DIR)/30-root-app.yaml"
	# 5. Wait for the root Application to become healthy. This is the
	#    operator-visible signal that GitOps has taken over.
	@echo "Waiting for root Application to sync …"
	@for i in $$(seq 1 60); do \
	  phase=$$(kubectl -n argocd get application root -o jsonpath='{.status.sync.status}' 2>/dev/null || echo ''); \
	  if [[ "$$phase" == "Synced" ]]; then echo "root Application Synced"; exit 0; fi; \
	  sleep 10; \
	done; \
	echo "root Application did not reach Synced state within 10m"; exit 1

# Dry-run what an Argo CD sync would do against the live cluster.
# Use before merging a change to see exactly what would change.
.PHONY: diff
diff: _validate-env
	@echo "Diff for env=$(ENV) against cluster $(CLUSTER_NAME):"
	argocd app diff platform-$(CLUSTER_NAME) || true
	argocd app diff observability-stack-$(CLUSTER_NAME) || true

# Force-sync (use sparingly — Argo CD will reconcile automatically).
.PHONY: sync
sync: _validate-env
	argocd app sync platform-$(CLUSTER_NAME)

# Print a human-readable status across all platform Applications.
.PHONY: status
status:
	@argocd app list -o wide | grep -E "platform|infra|observability" || true

.PHONY: help
help:
	@echo "Targets:"
	@echo "  bootstrap        One-time install of Argo CD + GitOps on a new cluster"
	@echo "  render           Render bootstrap manifests with substitutions resolved"
	@echo "  diff             Show Argo CD diff against live cluster"
	@echo "  sync             Force Argo CD sync of platform app"
	@echo "  status           Show status of all platform Applications"
	@echo ""
	@echo "Required env vars:"
	@echo "  REPO_URL=<git remote URL>"
	@echo "  REPO_REVISION=<branch|tag|sha> (default: main)"
	@echo "  CLUSTER_NAME=<cluster name as known to argocd>"
	@echo "  ENV=<dev|staging|production>"
