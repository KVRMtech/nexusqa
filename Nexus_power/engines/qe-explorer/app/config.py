"""QE-Central Contained Explorer — Configuration (env/config-driven).

Centralises every environment-driven knob for the ``qe-explorer`` service
(design §3.2). Mirrors the ``platform/qe-central/app/config.py`` idiom:
a ``pydantic_settings.BaseSettings`` subclass with per-field env aliases and
a module-level ``settings`` singleton read at import time.

The explorer sits on internal-only networks (design §1.1): it holds NO DB
creds and NO KMS. Its only secret is the per-fleet HMAC shared secret
``QEC_EXPLORER_TOKEN`` (the RUNNER_TOKEN pattern — runner_client.py:16), used
to authenticate inbound ``/api/v1/explore`` calls and to sign the completion
callback to qe-central. Defaults here are development-safe ONLY and MUST be
overridden by the ``docker-compose.qec.yml`` wiring in any deployed env.
"""
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

# The refuse pack ships INSIDE the package so the container always has a valid
# fail-closed default; ``REFUSE_PACK_PATH`` may point elsewhere for overrides.
_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_REFUSE_PACK_PATH = str(_PACKAGE_DIR / "refuse_pack.yaml")


class Settings(BaseSettings):
    """qe-explorer configuration loaded from environment variables.

    Env-var names are pinned by the Phase-1 shared conventions:
    ``QEC_EXPLORER_PORT``, ``QEC_CALLBACK_URL``, ``QEC_EXPLORER_TOKEN``,
    ``EGRESS_PROXY``, ``WORK_DIR``, ``REFUSE_PACK_PATH`` plus the crawl-budget
    defaults.
    """

    model_config = {"extra": "ignore", "populate_by_name": True}

    # ── Server ────────────────────────────────────────────────
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8210, alias="QEC_EXPLORER_PORT")
    service_name: str = Field(default="qe-explorer", alias="QEC_EXPLORER_SERVICE_NAME")
    log_level: str = Field(default="INFO", alias="QEC_EXPLORER_LOG_LEVEL")

    # ── Completion callback to qe-central (HMAC-signed) ───────
    #: Base URL of qe-central; the explorer POSTs the completion callback here.
    callback_url: str = Field(
        default="http://qe-central:8093", alias="QEC_CALLBACK_URL",
    )
    #: Path template for the completion callback (design §3.2 §API surface).
    callback_path_template: str = Field(
        default="/internal/crawls/{crawl_id}/complete",
        alias="QEC_CALLBACK_PATH_TEMPLATE",
    )

    # ── Per-fleet HMAC shared secret (RUNNER_TOKEN pattern) ───
    #: Authenticates inbound /api/v1/explore (X-QEC-Token) AND signs the
    #: completion callback body. Fail-closed: an empty secret can NEVER match.
    explorer_token: str = Field(
        default="dev-explorer-token-change-me", alias="QEC_EXPLORER_TOKEN",
    )

    # ── Network isolation (design §1.1) ───────────────────────
    #: The ONLY route to the internet — squid egress proxy. The browser is
    #: launched with ``--proxy-server=<this>`` and squid allowlists the host.
    egress_proxy: str = Field(
        default="http://qec-egress-proxy:3128", alias="EGRESS_PROXY",
    )

    # ── Filesystem (shared crawl-storage volume) ──────────────
    #: Explorer-side mount of the shared ``qec-crawl-storage`` volume;
    #: manifests + screenshots are written under ``{work_dir}/{crawl_id}/``.
    work_dir: str = Field(default="/work", alias="WORK_DIR")
    #: Absolute path to the versioned refuse pack (defaults to the packaged copy).
    refuse_pack_path: str = Field(
        default=_DEFAULT_REFUSE_PACK_PATH, alias="REFUSE_PACK_PATH",
    )

    # ── Crawl budgets (design §3.2 §API surface defaults) ─────
    max_states: int = Field(default=200, alias="QEC_MAX_STATES")
    max_depth: int = Field(default=6, alias="QEC_MAX_DEPTH")
    max_actions_per_state: int = Field(default=30, alias="QEC_MAX_ACTIONS_PER_STATE")
    max_wall_ms: int = Field(default=1_800_000, alias="QEC_MAX_WALL_MS")
    max_requests: int = Field(default=5000, alias="QEC_MAX_REQUESTS")
    rate_per_s: float = Field(default=1.0, alias="QEC_RATE_PER_S")

    # ── AUTH-phase window (the caller-enforced guard window, design §3.2) ─
    #: The login POST is permitted only within this narrow window after the
    #: login submit: at most ``auth_max_requests`` requests within
    #: ``auth_window_ms`` on the same registrable domain.
    auth_max_requests: int = Field(default=10, alias="QEC_AUTH_MAX_REQUESTS")
    auth_window_ms: int = Field(default=30_000, alias="QEC_AUTH_WINDOW_MS")
    #: Re-login attempts on session expiry (design §3.2 auth.py: ≤3).
    max_relogins: int = Field(default=3, alias="QEC_MAX_RELOGINS")
    #: Wizard/stepper traversal (#1) — advance non-danger Next/Continue on filled
    #: form states to record deeper steps.  Bounded + fingerprint-deduped +
    #: fail-closed (danger OR commit-word vetoes an advance).  ON by default; a
    #: per-deploy kill-switch for apps whose "Continue" is not vetted as reversible.
    wizard_enabled: bool = Field(default=True, alias="QEC_WIZARD_ENABLED")

    # ── E2E walk depth (per wizard chain / crawl-wide) ───────────────────
    #: How many steps ONE wizard chain may walk in End-to-end mode, and how
    #: many advances the whole crawl may make. These bound a COVERAGE walk,
    #: not a safety gate — the submit boundary, danger gate and commit veto
    #: are unaffected by them. Raise for deep funnels; every safety rule is
    #: identical at any depth.
    e2e_wizard_steps: int = Field(default=60, alias="QEC_E2E_WIZARD_STEPS")
    e2e_wizard_advances: int = Field(
        default=300, alias="QEC_E2E_WIZARD_ADVANCES")

    # ── E2E advance oracle resilience (agent-assisted wizard advance) ─────
    #: Per-call HTTP timeout for the pick-advance consultation. A stuck page is
    #: worth seconds, not half a minute — the honest ``oracle_unavailable``
    #: terminal makes fast failure safe.
    advance_oracle_timeout_s: float = Field(
        default=8.0, alias="QEC_ADVANCE_ORACLE_TIMEOUT_S")
    #: Consecutive unavailable outcomes that open the per-crawl circuit; once
    #: open, no further HTTP attempts are made for the remainder of the crawl.
    advance_oracle_breaker_threshold: int = Field(
        default=3, alias="QEC_ADVANCE_ORACLE_BREAKER_THRESHOLD")
    #: Hard cap on oracle HTTP calls per crawl — a pathological app cannot burn
    #: unbounded tokens. At the cap, consultations end ``unavailable`` (honest
    #: non-completing terminal), never silently "none".
    advance_oracle_max_calls: int = Field(
        default=200, alias="QEC_ADVANCE_ORACLE_MAX_CALLS")

    # ── Security helpers (the HMAC shared secret lives here) ──────────────

    def token_matches(self, provided: str | None) -> bool:
        """Constant-time comparison of an inbound token against the secret.

        Fail-closed: an empty configured secret OR an empty provided token can
        NEVER match, so a mis-provisioned deployment refuses every caller
        rather than authenticating with a blank credential.
        """
        configured = (self.explorer_token or "").strip()
        candidate = (provided or "").strip()
        if not configured or not candidate:
            return False
        return hmac.compare_digest(configured, candidate)

    def sign_payload(self, payload: bytes) -> str:
        """Hex HMAC-SHA256 of ``payload`` under the shared secret.

        Used to sign the completion callback body so qe-central can verify the
        callback originated from a trusted explorer (design §3.2 completion
        callback is "HMAC-signed").
        """
        secret = (self.explorer_token or "").encode("utf-8")
        return hmac.new(secret, payload or b"", hashlib.sha256).hexdigest()

    def budget_defaults(self) -> dict:
        """The crawl-budget dict in the manifest ``crawl_meta.budgets`` vocab.

        1:1 with design §3.2 ``budgets {max_states, max_depth,
        max_actions_per_state, max_wall_ms, max_requests, rate_per_s}`` so the
        crawler can seed an explore request from config without re-deriving the
        field names.
        """
        return {
            "max_states": self.max_states,
            "max_depth": self.max_depth,
            "max_actions_per_state": self.max_actions_per_state,
            "max_wall_ms": self.max_wall_ms,
            "max_requests": self.max_requests,
            "rate_per_s": self.rate_per_s,
        }

    def callback_path(self, crawl_id: str) -> str:
        """Render the completion-callback path for a crawl id."""
        return self.callback_path_template.format(crawl_id=crawl_id)


# Singleton — import as ``from .config import settings`` (relative) or
# ``from app.config import settings`` (absolute, e.g. tests).
settings = Settings()
