"""TEAM A / PHASE A — the crawl's PROXY IDENTITY (the explorer's fence half).

Frozen wire shape: ``Nexus_power/contracts/fleet_egress_fence_v1.json``.

The per-crawl egress fence works because squid can tell WHICH crawl a request
belongs to: every browser context is created with proxy credentials whose
USERNAME is the crawl id. squid authenticates with ``basic_fake_auth`` (any
password — the username is the whole identity; the password is a documented
constant, not a secret) and the generated ACL pair allows crawl X to reach
only the domains in crawl X's own fence file.

FAIL-CLOSED IN EVERY MIXED-VERSION DIRECTION:
  * new squid.conf + a context WITHOUT credentials → 407, reaches nothing;
  * old squid.conf (no auth) + credentials → squid ignores them, the legacy
    per-worker file still fences (today's behaviour, capacity 1);
  * a crawl id that cannot be a safe proxy login → refuse the dispatch here,
    before a browser exists, rather than launch it unfenced.

The launch-level ``--proxy-server`` stays exactly as it is; the per-context
proxy points at the SAME server and only adds the identity. (Playwright routes
per-context proxies through the launch proxy on Chromium, which is why the
launch proxy must remain configured — and it is, unchanged.)
"""
from __future__ import annotations

import re

#: The contract's constant password — basic_fake_auth accepts anything; a
#: per-crawl secret here would be theatre and is deliberately not pretended.
FENCE_PASSWORD = "fenced"

#: Mirror of the contract's crawl_id_pattern (and of qe-central's
#: egress_fence.CRAWL_ID_RE — asserted equal by the contract tests on each
#: side, since the services cannot import each other).
CRAWL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")


class FenceIdentityError(ValueError):
    """The crawl id cannot be a proxy login — the dispatch must be refused."""


def proxy_settings(crawl_id: str, egress_proxy: str) -> dict | None:
    """Playwright ``proxy`` kwargs for THIS crawl's browser context.

    ``None`` when no egress proxy is configured (a dev/test posture in which
    there is no fence to select — the guard's in-browser net still applies).
    Raises :class:`FenceIdentityError` for a crawl id that cannot safely be a
    proxy login, because launching that browser would mean launching it
    without a selectable fence.
    """
    server = (egress_proxy or "").strip()
    if not server:
        return None
    cid = (crawl_id or "").strip()
    if not CRAWL_ID_RE.match(cid):
        raise FenceIdentityError(
            f"crawl id {cid!r} cannot key the egress fence (proxy login)")
    return {"server": server, "username": cid, "password": FENCE_PASSWORD}
