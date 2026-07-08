"""QE-Central — Phase-1 explorer-dispatch configuration + HMAC contract.

The Phase-0 ``app/config.py`` singleton is left untouched; the Phase-1 wiring
(dispatch OUT to the contained explorer + verify the explorer's completion
callback IN) adds its own env-driven ``pydantic_settings`` block here so the
bounded Phase-0 surface is not disturbed.

Security posture (design §1.1 — fail-closed everywhere):
  * The explorer holds NO DB creds / NO KMS — its only credential is the
    per-fleet HMAC shared secret ``QEC_EXPLORER_TOKEN`` (RUNNER_TOKEN pattern).
  * Dispatch sends that secret as the ``X-QEC-Token`` header.
  * The completion callback is authenticated by an HMAC-SHA256 signature over
    the RAW request body, carried in the ``X-QEC-Signature`` header.
  * :meth:`Phase1Settings.sign_payload` MIRRORS the explorer's
    ``app/config.py::Settings.sign_payload`` BYTE-FOR-BYTE (same raw-token key
    derivation, same digest) so the two subsystems agree; a divergence would
    reject every genuine callback.
  * An empty secret can NEVER verify — a mis-provisioned deployment refuses
    every callback rather than trusting an unsigned one.
"""
from __future__ import annotations

import hashlib
import hmac

from pydantic import Field
from pydantic_settings import BaseSettings

#: The header carrying the shared secret on dispatch (explorer verifies it via
#: its own ``settings.token_matches``).
TOKEN_HEADER = "X-QEC-Token"
#: The header carrying the hex HMAC-SHA256 of the raw callback body.
SIGNATURE_HEADER = "X-QEC-Signature"


class Phase1Settings(BaseSettings):
    """Explorer-dispatch settings, loaded from the environment.

    Env-var names are pinned by the Phase-1 shared conventions; defaults are
    development-safe only and MUST be overridden by ``docker-compose.qec.yml``.
    """

    model_config = {"extra": "ignore", "populate_by_name": True}

    #: Base URL of the contained explorer on the internal ``qec-internal`` net.
    explorer_url: str = Field(
        default="http://qe-explorer:8210", alias="QEC_EXPLORER_URL",
    )
    #: Per-fleet HMAC shared secret (RUNNER_TOKEN pattern). Empty ⇒ fail-closed:
    #: dispatch and callback verification both refuse.
    explorer_token: str = Field(default="", alias="QEC_EXPLORER_TOKEN")
    #: Master switch for Phase-1 explorer dispatch. Default OFF keeps the
    #: Phase-0 inline-bundle path as the only active write path until an
    #: operator explicitly enables live crawling.
    dispatch_enabled: bool = Field(
        default=False, alias="QEC_EXPLORER_DISPATCH_ENABLED",
    )
    #: qe-central-side mount of the shared ``qec-crawl-storage`` volume; the
    #: explorer writes ``{root}/{crawl_id}/manifest.jsonl`` + staged PNGs here.
    crawl_storage_root: str = Field(
        default="/work", alias="QEC_CRAWL_STORAGE_ROOT",
    )
    #: The squid allowlist file qe-central populates before each dispatch
    #: (shared ``qec-egress-allowlist`` volume). See squid.conf.
    egress_allowlist_path: str = Field(
        default="/qec/egress-allowlist/allowed_domains.txt",
        alias="QEC_EGRESS_ALLOWLIST_PATH",
    )
    #: httpx dispatch timeout (seconds).
    dispatch_timeout_s: float = Field(
        default=30.0, alias="QEC_EXPLORER_DISPATCH_TIMEOUT_S",
    )
    #: httpx timeout for the E3 auth-import relay (seconds).
    auth_import_timeout_s: float = Field(
        default=30.0, alias="QEC_AUTH_IMPORT_TIMEOUT_S",
    )

    # ── HMAC helpers (MIRROR of the explorer sign contract) ───────────────

    def sign_payload(self, payload: bytes) -> str:
        """Hex HMAC-SHA256 of ``payload`` under the shared secret.

        BYTE-FOR-BYTE identical to the explorer's ``Settings.sign_payload``:
        the key is the RAW (un-stripped) ``explorer_token`` UTF-8 bytes.
        """
        secret = (self.explorer_token or "").encode("utf-8")
        return hmac.new(secret, payload or b"", hashlib.sha256).hexdigest()

    def verify_signature(self, payload: bytes, provided: str | None) -> bool:
        """Constant-time verify of a callback body signature (fail-closed).

        Returns ``False`` when the secret is unset or the header is absent, so
        an unsigned/mis-provisioned callback is never trusted.
        """
        if not (self.explorer_token or "").strip():
            return False
        candidate = (provided or "").strip()
        if not candidate:
            return False
        expected = self.sign_payload(payload)
        return hmac.compare_digest(expected, candidate)

    def token_value(self) -> str:
        """The ``X-QEC-Token`` value to send on dispatch (stripped)."""
        return (self.explorer_token or "").strip()


#: Singleton — import as ``from ..clients.config import phase1_settings``.
phase1_settings = Phase1Settings()
