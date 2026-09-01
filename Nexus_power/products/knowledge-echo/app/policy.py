"""Channel policy service.

The orchestrator consults this before dispatch. The policy can:

    * Force the effective mode to ``muted`` (no send) or
      ``shadow``/``dm_only``/``live`` independently of the tenant flag.
    * Raise (never lower) the confidence threshold required to post.
    * Apply a product / topic allowlist and blocklist.
    * Honor quiet-hours expressed in IANA timezones (e.g.
      ``America/New_York``).

Returned ``PolicyDecision`` carries an explicit ``allow`` plus a
``forced_mode`` (or None) and ``min_confidence_override`` (or None) so
the orchestrator can fold the override into its existing logic without
duplicating policy knowledge across modules.

Production behaviours:

    * Per-tenant TTL cache (60s) so the hot path doesn't query Postgres
      every echo.
    * Fail-open: when the DB is unreachable or the row is malformed
      we return ``allow=True`` with no overrides — never block a
      tenant on infrastructure trouble.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from .db import Database

logger = logging.getLogger(__name__)


_md = sa.MetaData()

surface_channel_policies = sa.Table(
    "surface_channel_policies",
    _md,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("surface", sa.String(32), primary_key=True),
    sa.Column("channel_id_ext", sa.String(128), primary_key=True),
    sa.Column("echo_mode", sa.String(16), nullable=False),
    sa.Column("min_confidence_override", sa.Float),
    sa.Column("allowlist_json", JSONB, nullable=False),
    sa.Column("blocklist_json", JSONB, nullable=False),
    sa.Column("quiet_hours_json", JSONB, nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


# ── DTOs ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class PolicyContext:
    tenant_id: str
    surface: str
    channel_id_ext: Optional[str]
    product_ids: tuple[str, ...] = ()
    topic_hints: tuple[str, ...] = ()
    now: Optional[datetime] = None


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    forced_mode: Optional[str] = None
    min_confidence_override: Optional[float] = None
    reason: str = ""

    @property
    def is_override(self) -> bool:
        return (
            self.forced_mode is not None
            or self.min_confidence_override is not None
            or not self.allow
        )


# ── Service ────────────────────────────────────────────────────


class ChannelPolicyService:
    def __init__(
        self,
        db: Database,
        *,
        cache_ttl_seconds: int = 60,
    ):
        self._db = db
        self._ttl = max(0, int(cache_ttl_seconds))
        self._cache: dict[tuple[str, str, str], tuple[Optional[dict], float]] = {}

    def invalidate(self, tenant_id: str) -> None:
        for k in list(self._cache.keys()):
            if k[0] == tenant_id:
                self._cache.pop(k, None)

    async def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        if not ctx.channel_id_ext:
            return PolicyDecision(allow=True, reason="no_channel")
        try:
            row = await self._load(
                ctx.tenant_id, ctx.surface, ctx.channel_id_ext
            )
        except Exception as exc:  # pragma: no cover — fail-open
            logger.warning("policy.load_failed: %s", exc)
            return PolicyDecision(allow=True, reason="policy_load_failed")
        if row is None:
            return PolicyDecision(allow=True, reason="no_policy_row")

        return _evaluate_row(row, ctx)

    # ── Internals ───────────────────────────────────────────────

    async def _load(
        self, tenant_id: str, surface: str, channel: str
    ) -> Optional[dict]:
        key = (tenant_id, surface, channel)
        cached = self._cache.get(key)
        if cached is not None:
            row, expires_at = cached
            if time.monotonic() < expires_at:
                return row
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(surface_channel_policies).where(
                        surface_channel_policies.c.tenant_id == tenant_id,
                        surface_channel_policies.c.surface == surface,
                        surface_channel_policies.c.channel_id_ext == channel,
                    )
                )
            ).mappings().first()
        materialised = dict(row) if row else None
        self._cache[key] = (materialised, time.monotonic() + self._ttl)
        return materialised


# ── Policy evaluation ─────────────────────────────────────────


def _evaluate_row(row: dict, ctx: PolicyContext) -> PolicyDecision:
    mode = (row.get("echo_mode") or "inherit").lower()
    if mode == "muted":
        return PolicyDecision(allow=False, reason="channel_muted")

    allow_block = _check_allow_block(row, ctx)
    if allow_block is not None:
        return allow_block

    quiet = _check_quiet_hours(row.get("quiet_hours_json") or {}, ctx.now)
    if quiet is not None:
        return quiet

    forced_mode: Optional[str] = None
    if mode in ("shadow", "dm_only", "live"):
        forced_mode = mode

    min_conf: Optional[float] = row.get("min_confidence_override")
    return PolicyDecision(
        allow=True,
        forced_mode=forced_mode,
        min_confidence_override=(
            float(min_conf) if min_conf is not None else None
        ),
        reason="policy_applied",
    )


def _check_allow_block(row: dict, ctx: PolicyContext) -> Optional[PolicyDecision]:
    allow = row.get("allowlist_json") or {}
    block = row.get("blocklist_json") or {}
    product_set = set(ctx.product_ids)
    topic_set = set(t.lower() for t in ctx.topic_hints)

    if isinstance(block, dict):
        bp = set(block.get("products") or [])
        bt = set(str(t).lower() for t in (block.get("topics") or []))
        if bp & product_set:
            return PolicyDecision(allow=False, reason="product_blocklisted")
        if bt & topic_set:
            return PolicyDecision(allow=False, reason="topic_blocklisted")

    if isinstance(allow, dict) and (allow.get("products") or allow.get("topics")):
        ap = set(allow.get("products") or [])
        at = set(str(t).lower() for t in (allow.get("topics") or []))
        if ap and not (ap & product_set):
            return PolicyDecision(allow=False, reason="product_not_in_allowlist")
        if at and not (at & topic_set):
            return PolicyDecision(allow=False, reason="topic_not_in_allowlist")
    return None


def _check_quiet_hours(
    quiet: dict[str, Any], now: Optional[datetime]
) -> Optional[PolicyDecision]:
    """Return suppression decision if 'now' falls inside a quiet window.

    ``quiet`` shape::

        {
          "timezone": "America/New_York",
          "windows": [
            {"days": ["mon", "tue", ...], "start": "18:00", "end": "08:00"}
          ]
        }
    """
    if not isinstance(quiet, dict):
        return None
    windows = quiet.get("windows")
    if not isinstance(windows, list) or not windows:
        return None

    tzname = quiet.get("timezone") or "UTC"
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tzname)
    except Exception:
        tz = timezone.utc

    now_aware = (now or datetime.now(timezone.utc)).astimezone(tz)
    day_name = now_aware.strftime("%a").lower()  # 'mon', 'tue', …
    minutes_now = now_aware.hour * 60 + now_aware.minute

    for w in windows:
        if not isinstance(w, dict):
            continue
        days = [str(d).lower() for d in (w.get("days") or [])]
        if days and day_name not in days:
            continue
        start_m = _parse_hhmm(w.get("start"))
        end_m = _parse_hhmm(w.get("end"))
        if start_m is None or end_m is None:
            continue
        # Overnight windows wrap around midnight (end < start).
        if start_m <= end_m:
            inside = start_m <= minutes_now < end_m
        else:
            inside = minutes_now >= start_m or minutes_now < end_m
        if inside:
            return PolicyDecision(
                allow=False,
                reason=f"quiet_hours:{w.get('start')}-{w.get('end')}",
            )
    return None


def _parse_hhmm(value: Any) -> Optional[int]:
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours < 24) or not (0 <= minutes < 60):
        return None
    return hours * 60 + minutes
