"""Strict manifest model for integration plugins.

Every integration ships a ``plugin.yaml`` (or ``plugin.json``) that
declares its capabilities, auth needs, events, and actions. Loading
the manifest is the only way the platform learns what an integration
is allowed to do — the runtime enforces these declarations.

Key invariants:

* The model is ``extra="forbid"`` end-to-end. Unknown keys are an
  immediate error — this prevents typos and feature creep through
  unreviewed manifest fields.
* IDs and slugs are constrained to ``[a-z0-9_.-]`` patterns. This
  matches the conventions in alembic migrations and is safe for
  metric labels and URL path segments.
* ``capabilities`` must be a non-empty subset of the enum. An
  integration with no declared capabilities is rejected.
* Each capability section is required iff that capability is declared.
* Outbound actions and inbound events carry an ``id`` unique within
  their scope so observability can label metrics by event/action.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ── Errors ──────────────────────────────────────────────────────


class ManifestError(Exception):
    """Raised by ``load_manifest`` on validation failure."""

    def __init__(self, source: str, message: str):
        self.source = source
        super().__init__(f"manifest error in {source}: {message}")


# ── Enums ───────────────────────────────────────────────────────


class Capability(str, Enum):
    SOURCE = "source"
    SURFACE = "surface"
    SINK = "sink"


class AuthMethod(str, Enum):
    OAUTH2 = "oauth2"
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"
    HMAC_SIGNED_WEBHOOK = "hmac_signed_webhook"
    BASIC = "basic"
    NONE = "none"


# ── Reusable building blocks ───────────────────────────────────


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SLUG_MAX = 128


def _validate_id(value: str, field_name: str) -> str:
    if not _ID_PATTERN.match(value):
        raise ValueError(
            f"{field_name}={value!r} must match {_ID_PATTERN.pattern}"
        )
    return value


class RateLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    per_second: Optional[int] = Field(default=None, ge=1, le=10_000)
    per_minute: Optional[int] = Field(default=None, ge=1, le=600_000)
    per_hour: Optional[int] = Field(default=None, ge=1, le=36_000_000)
    burst: Optional[int] = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def _at_least_one(self) -> "RateLimit":
        if (
            self.per_second is None
            and self.per_minute is None
            and self.per_hour is None
        ):
            raise ValueError(
                "rate_limit must declare at least one of "
                "per_second/per_minute/per_hour"
            )
        return self


class HealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=512)
    interval_seconds: int = Field(default=60, ge=10, le=3600)
    timeout_seconds: int = Field(default=5, ge=1, le=60)


class AuthSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: AuthMethod
    scopes: list[str] = Field(default_factory=list, max_length=64)
    per_tenant: bool = True
    redirect_url_template: Optional[str] = Field(
        default=None, max_length=512
    )
    rotation_period_days: Optional[int] = Field(
        default=None, ge=1, le=365
    )

    @field_validator("scopes")
    @classmethod
    def _scopes_unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("scopes must be unique")
        for scope in v:
            if not scope or len(scope) > 128:
                raise ValueError(
                    "each scope must be 1..128 chars"
                )
        return v


class EventSubscription(BaseModel):
    """Declarative inbound event the integration listens for."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=128)
    handler: str = Field(min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)

    @field_validator("id", "type", "handler")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        return _validate_id(v, "field")


class Action(BaseModel):
    """Declarative outbound action the integration can perform."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    required_params: list[str] = Field(default_factory=list, max_length=32)
    optional_params: list[str] = Field(default_factory=list, max_length=32)
    idempotent: bool = False
    retries: int = Field(default=3, ge=0, le=10)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        return _validate_id(v, "action.id")

    @field_validator("required_params", "optional_params")
    @classmethod
    def _valid_params(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p or len(p) > 64:
                raise ValueError(
                    "each param name must be 1..64 chars"
                )
        if len(set(v)) != len(v):
            raise ValueError("param names must be unique")
        return v


# ── Capability sections ────────────────────────────────────────


class SourceCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[EventSubscription] = Field(min_length=1, max_length=64)
    rate_limit: Optional[RateLimit] = None

    @field_validator("events")
    @classmethod
    def _unique_event_ids(
        cls, v: list[EventSubscription]
    ) -> list[EventSubscription]:
        ids = [e.id for e in v]
        if len(set(ids)) != len(ids):
            raise ValueError("source.events ids must be unique")
        return v


class SurfaceCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inbound: list[EventSubscription] = Field(default_factory=list, max_length=64)
    outbound: list[Action] = Field(default_factory=list, max_length=64)
    rate_limit: Optional[RateLimit] = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "SurfaceCapability":
        if not self.inbound and not self.outbound:
            raise ValueError(
                "surface capability must declare inbound or outbound"
            )
        return self

    @field_validator("inbound")
    @classmethod
    def _unique_inbound_ids(
        cls, v: list[EventSubscription]
    ) -> list[EventSubscription]:
        ids = [e.id for e in v]
        if len(set(ids)) != len(ids):
            raise ValueError("surface.inbound ids must be unique")
        return v

    @field_validator("outbound")
    @classmethod
    def _unique_outbound_ids(cls, v: list[Action]) -> list[Action]:
        ids = [a.id for a in v]
        if len(set(ids)) != len(ids):
            raise ValueError("surface.outbound ids must be unique")
        return v


class SinkCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[Action] = Field(min_length=1, max_length=64)
    rate_limit: Optional[RateLimit] = None

    @field_validator("actions")
    @classmethod
    def _unique_action_ids(cls, v: list[Action]) -> list[Action]:
        ids = [a.id for a in v]
        if len(set(ids)) != len(ids):
            raise ValueError("sink.actions ids must be unique")
        return v


# ── Routing ────────────────────────────────────────────────────


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_resolver: str = Field(min_length=1, max_length=128)
    tenant_cache_seconds: int = Field(default=300, ge=0, le=3600)


# ── Top-level manifest ─────────────────────────────────────────


class IntegrationManifest(BaseModel):
    """Top-level manifest schema.

    A valid manifest declares ``capabilities`` and supplies the matching
    section(s). Validators enforce cross-field consistency: if
    ``Capability.SOURCE`` is declared then ``source`` must be non-null
    (and so on); declaring a capability without its section is rejected,
    as is providing a section for an undeclared capability.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
    display_name: str = Field(min_length=1, max_length=256)
    vendor: str = Field(min_length=1, max_length=128)
    tier: str = Field(
        default="standard",
        pattern=r"^(standard|enterprise|sovereign|community)$",
    )

    capabilities: list[Capability] = Field(min_length=1, max_length=3)

    auth: AuthSpec

    source: Optional[SourceCapability] = None
    surface: Optional[SurfaceCapability] = None
    sink: Optional[SinkCapability] = None

    routing: RoutingConfig
    health: HealthCheck

    min_platform_version: str = Field(
        default="0.1.0",
        pattern=r"^\d+\.\d+\.\d+$",
    )
    tags: list[str] = Field(default_factory=list, max_length=32)
    documentation_url: Optional[str] = Field(default=None, max_length=512)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        return _validate_id(v, "id")

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, v: list[str]) -> list[str]:
        for tag in v:
            if not _ID_PATTERN.match(tag):
                raise ValueError(
                    f"tag {tag!r} must match {_ID_PATTERN.pattern}"
                )
        if len(set(v)) != len(v):
            raise ValueError("tags must be unique")
        return v

    @field_validator("capabilities")
    @classmethod
    def _unique_capabilities(
        cls, v: list[Capability]
    ) -> list[Capability]:
        if len(set(v)) != len(v):
            raise ValueError("capabilities must be unique")
        return v

    @model_validator(mode="after")
    def _capability_sections_align(self) -> "IntegrationManifest":
        declared = set(self.capabilities)
        provided = {
            Capability.SOURCE: self.source is not None,
            Capability.SURFACE: self.surface is not None,
            Capability.SINK: self.sink is not None,
        }
        for cap, has_section in provided.items():
            if has_section and cap not in declared:
                raise ValueError(
                    f"capability {cap.value} section provided but not declared"
                )
            if cap in declared and not has_section:
                raise ValueError(
                    f"capability {cap.value} declared but section missing"
                )
        return self


# ── Loader ─────────────────────────────────────────────────────


def load_manifest(path: str | Path) -> IntegrationManifest:
    """Load and validate a manifest from a YAML or JSON file.

    Raises ``ManifestError`` for any failure: file missing, parse
    error, schema violation, or cross-field validation failure.
    """
    p = Path(path)
    if not p.exists():
        raise ManifestError(str(p), "file not found")
    if not p.is_file():
        raise ManifestError(str(p), "not a regular file")

    raw = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()

    try:
        if suffix in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore[import-untyped]
            except ImportError as exc:
                raise ManifestError(
                    str(p),
                    "PyYAML is required to load .yaml manifests",
                ) from exc
            data = yaml.safe_load(raw)
        elif suffix == ".json":
            data = json.loads(raw)
        else:
            raise ManifestError(
                str(p), f"unsupported manifest extension: {suffix}"
            )
    except ManifestError:
        raise
    except Exception as exc:
        raise ManifestError(str(p), f"parse failed: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError(
            str(p), f"manifest root must be a mapping, got {type(data).__name__}"
        )

    try:
        return IntegrationManifest.model_validate(data)
    except Exception as exc:
        raise ManifestError(str(p), str(exc)) from exc


def json_schema() -> dict:
    """Return the JSON Schema (draft 2020-12) for the manifest.

    Customers writing plugins can validate their manifests offline
    against this schema. The schema is generated from the Pydantic
    model so it stays in lock-step with the runtime contract.
    """
    return IntegrationManifest.model_json_schema()
