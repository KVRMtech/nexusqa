"""RUNG 2, THE DOORS — how a client's environment is actually reached.

:mod:`app.services.env_data` decides WHICH slot a field maps to and refuses an
ambiguous one. This module decides HOW the answer is fetched, and nothing here
may loosen a rule made there: every transport satisfies the same tiny protocol
(``slots()`` / ``value(slot_key)``) and the resolver is unchanged by which one a
tenant configured.

THREE DOORS BECAUSE CLIENTS DIFFER, NOT BECAUSE THE LADDER DOES.

  * ``manifest``  a file the client exported once. No network, no credential,
                  nothing to keep running — reach for it first. It is
                  :class:`app.services.env_data.StaticProvider`, so it needs no
                  code here at all.
  * ``rest``      a URL this service asks per slot. The common enterprise door:
                  most test environments already have a fixture endpoint, and
                  the ask is a read-only token rather than a data export.
  * ``mcp``       an MCP endpoint, for clients who already run one. Deliberately
                  a THIN wrapper over the REST shape rather than a second
                  client: the transport differs, the contract does not.

WHY EVERY FAILURE IS A DECLINE, NEVER AN EXCEPTION. A client's test environment
is somebody else's system: it restarts, it rate-limits, its certificate expires
on a Sunday. If any of that could stop a crawl, this rung would make the product
LESS reliable than not having it — the exact opposite of its purpose. So each
transport catches its own failures and returns ``None``, and the value simply
falls to the rungs below.

THE OUTBOUND CALL IS TO THE CLIENT'S OWN SYSTEM, WHICH IS WHY IT IS ALLOWED.
This is not third-party egress: the destination is a URL the tenant themselves
registered, reached with a credential they themselves issued, to fetch data they
themselves own. Nothing tenant-crossing leaves this process — the field LABEL is
sent, never a value, and never another tenant's anything. The base URL is bound
per provider instance by the caller that read the tenant's own configuration,
so a slot key can never redirect the request somewhere else.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote

import httpx

from .env_data import KIND_MANIFEST, KIND_MCP, KIND_REST, StaticProvider

logger = logging.getLogger(__name__)

#: Short, because a rung that declines is cheap and a crawl that waits is not.
#: A client's fixture endpoint that cannot answer in this long is one the crawl
#: is better off stepping past than blocking on.
DEFAULT_TIMEOUT_S = 5.0

#: Nothing is worth more than this from one environment. A misconfigured
#: endpoint returning a megabyte is a decline, not a value pasted into a field.
MAX_VALUE_CHARS = 512


def _clean(value: Any) -> Optional[str]:
    """One rule for what an environment is allowed to have said.

    Applied to every transport's response, so a new door cannot arrive with a
    looser idea of what counts as an answer.
    """
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_VALUE_CHARS:
        return None
    return text


@dataclass
class RestProvider:
    """A client test environment answering over HTTP.

    The contract asked of the client is deliberately as small as it can be::

        GET {base_url}/slots            -> {"slots": ["member id", ...]}
        GET {base_url}/value/{slot_key} -> {"value": "M-1001"}

    Two read-only endpoints, no schema to agree, no export to maintain. That
    smallness is the point — it is what makes "give us a URL and a token" a
    request a client's platform team can actually say yes to in one meeting.
    """

    base_url: str
    token: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    #: Injected by the tests; production leaves it None and a client is built
    #: per call, matching how every other outbound call in this service works.
    client: Optional[httpx.Client] = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _get(self, path: str) -> Optional[dict]:
        url = self.base_url.rstrip("/") + path
        try:
            if self.client is not None:
                resp = self.client.get(url, headers=self._headers(),
                                       timeout=self.timeout_s)
            else:
                with httpx.Client(timeout=self.timeout_s) as client:
                    resp = client.get(url, headers=self._headers())
            if resp.status_code != 200:
                logger.info("qec.env_data.rest_status status=%d", resp.status_code)
                return None
            body = resp.json()
            return body if isinstance(body, dict) else None
        except Exception:                                        # noqa: BLE001
            # The client's environment is not ours to keep alive. Decline.
            logger.info("qec.env_data.rest_unreachable")
            return None

    def slots(self) -> Sequence[str]:
        body = self._get("/slots") or {}
        raw = body.get("slots")
        if not isinstance(raw, list):
            return []
        return [str(s) for s in raw if isinstance(s, str) and s.strip()]

    def value(self, slot_key: str) -> Optional[str]:
        # QUOTED because a slot key is human-assigned text ("member id") and
        # could otherwise steer the path. The base URL is bound per instance,
        # so the destination cannot be changed by what a slot is called.
        body = self._get("/value/" + quote(str(slot_key), safe="")) or {}
        return _clean(body.get("value"))


@dataclass
class McpProvider:
    """An MCP endpoint, wrapping the same two reads.

    A THIN wrapper on purpose. MCP is a transport a client may already run, not
    a different contract, and modelling it as one would give this rung two sets
    of rules to keep in step. If a client speaks MCP, they get the same two
    questions asked over their preferred pipe.
    """

    endpoint: str
    token: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    client: Optional[httpx.Client] = None

    def __post_init__(self) -> None:
        self._rest = RestProvider(self.endpoint, self.token, self.timeout_s,
                                  self.client)

    def slots(self) -> Sequence[str]:
        return self._rest.slots()

    def value(self, slot_key: str) -> Optional[str]:
        return self._rest.value(slot_key)


def build(config: Mapping[str, Any]) -> Optional[Any]:
    """The tenant's stored configuration -> a provider, or None.

    Returns None rather than raising for anything unrecognised or incomplete: a
    half-configured environment must leave the ladder exactly as it was, not
    fail a dispatch. An operator sees the misconfiguration in the data account's
    provenance counts — no values arrived from ``env`` — which is a truer signal
    than a 500 at schedule time.
    """
    kind = str((config or {}).get("kind") or "").strip().lower()

    if kind == KIND_MANIFEST:
        values = (config or {}).get("values")
        if not isinstance(values, dict):
            return None
        clean = {}
        for key, raw in values.items():
            cleaned = _clean(raw)
            if cleaned:
                clean[str(key)] = cleaned
        return StaticProvider(clean) if clean else None

    if kind in (KIND_REST, KIND_MCP):
        url = str((config or {}).get("base_url")
                  or (config or {}).get("endpoint") or "").strip()
        if not url.startswith(("http://", "https://")):
            return None
        token = str((config or {}).get("token") or "")
        cls = RestProvider if kind == KIND_REST else McpProvider
        return cls(url, token)

    return None
