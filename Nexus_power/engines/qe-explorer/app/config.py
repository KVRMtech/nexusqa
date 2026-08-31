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

from . import hmac_auth
from .hmac_auth import KeyRing

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
    #: NO DEFAULT (M0.5 T-SEC-01): a shipped development secret means a fresh
    #: deployment authenticates with a value that is in this repository. Empty ⇒
    #: fail-closed — ``token_matches`` never matches and signing raises, so the
    #: explorer refuses every caller rather than trusting a known credential.
    explorer_token: str = Field(default="", alias="QEC_EXPLORER_TOKEN")
    #: ROTATION SEAM (T-SEC-11) — the PREVIOUS fleet secret, accepted for
    #: verification until ``explorer_token_previous_expires_at`` (epoch SECONDS).
    explorer_token_previous: str = Field(
        default="", alias="QEC_EXPLORER_TOKEN_PREVIOUS",
    )
    explorer_token_previous_expires_at: float = Field(
        default=0.0, alias="QEC_EXPLORER_TOKEN_PREVIOUS_EXPIRES_AT",
    )
    #: Allowed clock skew (seconds, both directions) on a signed payload.
    hmac_skew_seconds: float = Field(
        default=float(hmac_auth.DEFAULT_SKEW_SECONDS),
        alias="QEC_HMAC_SKEW_SECONDS",
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

    #: BRANCH COVERAGE. Off by default: a sweep re-loads the page once per
    #: answer, so on a 60-question page it is the most expensive thing the crawl
    #: can do, and an operator must ask for it. See app/branch_walk.py.
    branch_sweep: bool = Field(default=False, alias="QEC_BRANCH_SWEEP")
    #: Ceiling on (question, answer) pairs visited per page.
    branch_max_visits: int = Field(default=400, alias="QEC_BRANCH_MAX_VISITS")
    #: How deep a reveal chain is followed. Level 0 is the page as it loaded.
    branch_max_depth: int = Field(default=6, alias="QEC_BRANCH_MAX_DEPTH")

    #: RUNG 8 — the LLM data agent. Off by default: the operator chooses it in
    #: the portal (LLM / User+LLM data modes). Values are provenance-stamped
    #: "llm" so a model's plausible answer is never confused with the client's.
    data_llm: bool = Field(default=False, alias="QEC_DATA_LLM")
    #: Model for the data agent (OpenAI chat completions).
    data_llm_model: str = Field(default="gpt-4o-mini", alias="QEC_DATA_LLM_MODEL")
    #: Per-crawl ceiling on agent calls — a pathological form cannot spend
    #: unbounded tokens.
    data_llm_max_calls: int = Field(default=150, alias="QEC_DATA_LLM_MAX_CALLS")
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
    #: How long to wait before re-reading a page that offered NO field and NO
    #: button — an async decision step still rendering "Processing...".
    #: MEASURED on vkpowerlife 2026-08-30: networkidle at 670ms, the forward
    #: control at 2857ms, so 3000 leaves headroom over the real gap. 0 disables.
    undecided_settle_ms: int = Field(default=3_000,
                                     alias="QEC_UNDECIDED_SETTLE_MS")
    #: B1-S — how many times the rejection reader may step BACK through a
    #: multi-step form after a commit that was refused silently, looking for the
    #: step the refused field (and therefore its message node) lives on.
    #: MEASURED on summit-life-carrier 2026-08-31: the wizard's review step
    #: renders no message node at all and its four field steps sit 1-4 clicks
    #: behind it, so 4 reaches every one of them. 0 disables the mechanism
    #: entirely and restores the previous behaviour exactly.
    step_back_max: int = Field(default=4, alias="QEC_STEP_BACK_MAX")
    #: Re-login attempts on session expiry (design §3.2 auth.py: ≤3).
    max_relogins: int = Field(default=3, alias="QEC_MAX_RELOGINS")

    # ── M1.3 CONTROLLED WALK PERSISTENCE (T-WP-01 / T-WP-02) ─────────────
    # There is deliberately NO enable/disable flag here.  The capability is
    # switched on by the arrival of a cryptographically valid, platform-issued
    # provisioning proof and by nothing else — an operator cannot turn walk
    # mutation on, and cannot turn the verification off.  Absent public keys or
    # an absent issuer means the trust store is unconfigured, which means every
    # walk mutation is refused: that is the shipped default.
    #
    #: Comma-separated base64 raw-32-byte Ed25519 PUBLIC keys of the platform
    #: attestation issuer.  VERIFICATION ONLY — this service never holds the
    #: private half, so a full compromise of the explorer cannot mint a proof.
    attestation_public_keys: str = Field(
        default="", alias="QEC_ATTESTATION_PUBLIC_KEYS")
    #: The issuer identity every proof's ``issuer`` claim must equal.  Empty ⇒
    #: the trust store is unconfigured ⇒ walk mutation is off.
    attestation_issuer: str = Field(default="", alias="QEC_ATTESTATION_ISSUER")
    #: Verifier-enforced ceiling on a proof's lifetime.  Bounds the window in
    #: which a stolen proof is useful, independently of what the issuer minted.
    attestation_max_lifetime_ms: int = Field(
        default=86_400_000, alias="QEC_ATTESTATION_MAX_LIFETIME_MS")
    #: Allowed issuer↔verifier clock skew, both directions (epoch ms).
    attestation_skew_ms: int = Field(
        default=300_000, alias="QEC_ATTESTATION_SKEW_MS")
    #: FLEET CEILING on mutations per logical wizard step.  The effective budget
    #: is ``min(this, the proof's own request)`` — a proof can ask for less, and
    #: can never ask for more.
    walk_max_mutations_per_step: int = Field(
        default=3, alias="QEC_WALK_MAX_MUTATIONS_PER_STEP")
    #: How long the actuation window around ONE walk click stays open (ms).
    #: Bounds a Save-Draft burst without admitting background autosave.
    walk_mutation_window_ms: int = Field(
        default=15_000, alias="QEC_WALK_MUTATION_WINDOW_MS")

    def attestation_trust_store(self):
        """Build the :class:`app.attest.TrustStore` for this fleet.

        Fail-closed by construction: no keys or no issuer ⇒ ``configured`` is
        False ⇒ :func:`app.attest.verify_provisioning_proof` denies everything
        with ``no_trust_anchor``."""
        from .attest import TrustStore
        keys = [k.strip() for k in (self.attestation_public_keys or "").split(",")
                if k.strip()]
        return TrustStore.from_public_keys(
            keys, issuer=self.attestation_issuer,
            max_lifetime_ms=int(self.attestation_max_lifetime_ms),
            skew_ms=int(self.attestation_skew_ms),
            max_mutations_per_step=int(self.walk_max_mutations_per_step),
        )
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
    medic_oracle_timeout_s: float = Field(
        default=10.0, alias="QEC_MEDIC_ORACLE_TIMEOUT_S")
    medic_oracle_breaker_threshold: int = Field(
        default=3, alias="QEC_MEDIC_BREAKER_THRESHOLD")
    medic_oracle_max_calls: int = Field(
        default=50, alias="QEC_MEDIC_MAX_CALLS")

    # ── M3.1 / T-VIS-03 · VISION'S OWN BUDGET ─────────────────────────────
    # These three used to be the MEDIC's. ``_make_vision_oracle`` spent
    # ``medic_oracle_max_calls`` / ``medic_oracle_timeout_s`` /
    # ``medic_oracle_breaker_threshold``, so a canvas application that burned ten
    # perceive calls silently took ten repair calls away from the interaction
    # ladder, and a vision provider outage opened the breaker the DOM medic was
    # relying on. Two capabilities with different cost profiles, different
    # failure modes and different blast radii cannot share one budget.
    #
    # qe-central has declared ``vision_max_calls`` / ``vision_breaker_threshold``
    # since the flag was written and NOTHING read them; these are the reader.
    #: Hard cap on vision (perceive) calls per crawl. Each is a multimodal call
    #: over a full-page screenshot — the most expensive thing this engine does.
    vision_oracle_max_calls: int = Field(
        default=10, alias="QEC_VISION_MAX_CALLS")
    #: Per-call HTTP timeout for a vision consultation. Longer than the medic's:
    #: a multimodal request over an 8 MiB image legitimately takes longer than a
    #: text repair, and timing it out at the text budget would report a working
    #: provider as unavailable.
    vision_oracle_timeout_s: float = Field(
        default=25.0, alias="QEC_VISION_ORACLE_TIMEOUT_S")
    #: Consecutive vision failures that open vision's OWN breaker. Once open it
    #: stays open for the crawl — fail-closed, no half-open probe.
    vision_oracle_breaker_threshold: int = Field(
        default=3, alias="QEC_VISION_BREAKER")
    #: How many coordinate actions one state may spend on perceived controls.
    #: Bounds a hallucinated 40-control perception into a handful of clicks.
    vision_max_actions_per_state: int = Field(
        default=2, alias="QEC_VISION_MAX_ACTIONS_PER_STATE")

    # ── Security helpers (the HMAC shared secret lives here) ──────────────

    def keyring(self) -> KeyRing:
        """The keys this explorer will accept/sign with (current + overlap)."""
        return KeyRing(
            current=self.explorer_token or "",
            previous=self.explorer_token_previous or "",
            previous_expires_at=float(self.explorer_token_previous_expires_at or 0.0),
        )

    def token_matches(self, provided: str | None) -> bool:
        """Constant-time comparison of an inbound token against the secret.

        Fail-closed: an empty configured secret OR an empty provided token can
        NEVER match, so a mis-provisioned deployment refuses every caller
        rather than authenticating with a blank credential.

        ROTATION (T-SEC-11): the PREVIOUS secret is also accepted while its
        overlap window is open, so a qe-central that has not yet been restarted
        with the new key is not locked out mid-rotation.
        """
        import time as _time

        configured = (self.explorer_token or "").strip()
        candidate = (provided or "").strip()
        if not configured or not candidate:
            return False
        if hmac.compare_digest(configured, candidate):
            return True
        previous = (self.explorer_token_previous or "").strip()
        if previous and _time.time() <= float(self.explorer_token_previous_expires_at or 0.0):
            return hmac.compare_digest(previous, candidate)
        return False

    def sign_payload(self, payload: bytes, *, scope: str = "") -> str:
        """The v2 ``X-QEC-Signature`` envelope for ``payload`` (key id +
        timestamp + single-use nonce + body hash + scope).

        MIRRORS qe-central's ``Phase1Settings.sign_payload`` byte-for-byte (both
        delegate to the duplicated :mod:`hmac_auth`).  ``scope`` binds the
        signature to one logical operation so a captured signature cannot
        authenticate a call about a different crawl.
        """
        return hmac_auth.sign(payload, keyring=self.keyring(), scope=scope)

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
