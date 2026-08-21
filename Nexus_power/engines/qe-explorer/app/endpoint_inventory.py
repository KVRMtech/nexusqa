"""M2.5 / T-NET-04 — the application's API surface, aggregated from observed calls.

A raw network stream answers "what happened during this crawl".  An endpoint
inventory answers "what API does this application have" — which is the question
the catalog and the compiler need, and the one a list of raw URLs cannot answer
because it is a list of the particular records the crawl happened to touch.

The aggregation key is ``method x path_template``, so
``GET /api/policies/8837`` and ``GET /api/policies/9021`` are ONE endpoint
observed twice, while ``POST /api/policies`` is a different one.

Pure + stdlib-only, and deliberately narrower than the raw stream it reads:

* **No raw request data enters the inventory.**  No URLs with identifiers, no
  header values, no body values.  Body evidence is reduced to KEY NAMES (already
  masked where the name is itself a secret) — the API contract, never the user's
  data.  A catalog is a durable, widely-read artifact; the raw stream is
  per-crawl evidence with a tighter blast radius, and the two must not be
  conflated.
* **Counts are observations, not assertions.**  ``observed_count`` is how many
  times the crawl saw the call; ``statuses`` is every distinct status with its
  own count, so three 503s followed by a 200 are legible as a retry that
  eventually succeeded rather than as "this endpoint returns 200".
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from . import network_evidence as ne

#: Cap on distinct endpoints in one inventory.  A runaway SPA must not be able to
#: grow the catalog without bound; reaching the cap is REPORTED (see
#: ``truncated`` on the result) so a clipped inventory never reads as complete.
MAX_ENDPOINTS = 200
#: Cap on distinct UI actions recorded per endpoint.
MAX_ACTIONS_PER_ENDPOINT = 12
#: Cap on distinct body key names carried per endpoint.
MAX_KEYS_PER_ENDPOINT = 40


def _int_or_zero(value: Any) -> int:
    try:
        return int(str(value or "0").strip() or 0)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Any) -> int | None:
    """An integer field that may legitimately be ABSENT.

    Distinct from :func:`_int_or_zero` on purpose: ``sequence`` and
    ``timestamp_ms`` use ``None`` to mean "not observed", and folding that to 0
    would claim an endpoint was first seen at ordinal zero.

    Accepts a string as well as an int, and that is the whole point — see
    :func:`build_inventory`'s note on reading an event back off a manifest.
    """
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _status_class(status: int) -> str:
    """The status FAMILY, so an inventory row is legible at a glance."""
    if status <= 0:
        return "failed"
    return f"{status // 100}xx"


def build_inventory(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate raw network events into an application-level endpoint inventory.

    Returns ``{"endpoints": [...], "endpoint_count": n, "event_count": n,
    "truncated": bool}``.  Each endpoint row carries:

    ``method``, ``path_template``, ``host``, ``auth_pattern``, ``response_shape``,
    ``statuses`` (distinct status -> count), ``status_classes``,
    ``observed_count``, ``first_sequence`` / ``last_sequence``,
    ``first_timestamp_ms`` / ``last_timestamp_ms``, ``request_keys``,
    ``resource_types``, ``actions`` (the UI actions observed to trigger it),
    ``has_server_error``, ``retried``, ``rate_limited``.

    ``retried`` and ``rate_limited`` are DERIVED from what was observed, not
    from a rule about what ought to happen: an endpoint is ``retried`` when the
    crawl saw the same method+template more than once within one action, and
    ``rate_limited`` when a 429 was observed.  Both would be invisible in the
    baseline, where repeated calls were deduplicated away.
    """
    rows: dict[str, dict[str, Any]] = {}
    event_count = 0
    truncated = False
    #: (endpoint key, action token) pairs seen — how ``retried`` is derived.
    per_action_counts: dict[tuple[str, str], int] = {}

    # GATE 3 / A23 — AGGREGATE IN CAPTURE ORDER, NOT ARRIVAL ORDER.
    #
    # A23 requires the action↔endpoint join to be DETERMINISTIC, and it was not:
    # feeding the same 68 live events in a different order produced a different
    # inventory. Endpoint identity, counts and statuses were stable; the
    # `actions` list was not, and for three of the seven endpoints the shuffled
    # run kept a DIFFERENT SET — because MAX_ACTIONS_PER_ENDPOINT is a prefix cap
    # and a prefix of an unordered stream is arbitrary.
    #
    # Every event already carries `sequence`, the crawl-wide ordinal assigned at
    # capture (T-NET-02) — it exists precisely so order can be recovered after
    # the fact. Sorting by it makes the whole aggregation a function of the event
    # SET rather than of how it happened to be delivered, which in turn makes the
    # cap keep the first-observed actions rather than the first-delivered ones.
    #
    # Stable and total: events with no readable sequence keep their relative
    # order and sort after everything that has one, so an inventory built from a
    # source that never assigned ordinals behaves exactly as it did before.
    ordered = sorted(
        enumerate(e for e in (events or ()) if isinstance(e, Mapping)),
        key=lambda pair: (
            _int_or_none(pair[1].get("sequence")) is None,
            _int_or_none(pair[1].get("sequence")) or 0,
            pair[0],
        ),
    )

    for _index, event in ordered:
        if not isinstance(event, Mapping):
            continue
        url = str(event.get("url") or "").strip()
        if not url:
            continue
        parts = urlsplit(url)
        if (parts.scheme or "").lower() not in ("http", "https", "ws", "wss"):
            continue
        event_count += 1

        method = str(event.get("method") or "").strip().upper()[:10]
        template = ne.path_template(parts.path)
        host = (parts.netloc or "")[:200]
        key = f"{method} {host}{template}"

        row = rows.get(key)
        if row is None:
            if len(rows) >= MAX_ENDPOINTS:
                truncated = True
                continue
            row = rows[key] = {
                "method": method,
                "path_template": template,
                "host": host,
                "auth_pattern": "none",
                "response_shape": "",
                "statuses": {},
                "status_classes": [],
                "observed_count": 0,
                "first_sequence": None,
                "last_sequence": None,
                "first_timestamp_ms": None,
                "last_timestamp_ms": None,
                "request_keys": [],
                "resource_types": [],
                "actions": [],
                "has_server_error": False,
                "retried": False,
                "rate_limited": False,
            }

        row["observed_count"] += 1

        status = _int_or_zero(event.get("status"))
        status_key = str(status) if status else "failed"
        row["statuses"][status_key] = row["statuses"].get(status_key, 0) + 1
        klass = _status_class(status)
        if klass not in row["status_classes"]:
            row["status_classes"].append(klass)
        if 500 <= status <= 599:
            row["has_server_error"] = True
        if status == 429:
            row["rate_limited"] = True

        # Auth pattern: the STRONGEST pattern ever observed on this endpoint.
        # An endpoint called once anonymously and once with a bearer token is an
        # authenticated endpoint that also has an anonymous path — reporting
        # "none" because the last call was anonymous would be the wrong way round.
        observed_auth = str(event.get("auth_pattern") or "").strip() or ne.auth_pattern(
            event.get("request_headers") or {})
        rank = {"none": 0, "cookie": 1, "api_key": 2, "authorization": 3,
                "basic": 4, "bearer": 5}
        if rank.get(observed_auth, 0) > rank.get(row["auth_pattern"], 0):
            row["auth_pattern"] = observed_auth

        shape = str(event.get("response_shape") or "").strip()
        if shape and shape not in ("unknown", "empty"):
            row["response_shape"] = shape
        elif shape and not row["response_shape"]:
            row["response_shape"] = shape

        # GATE 3 / A23 — PARSED, NOT ``isinstance``-CHECKED.
        #
        # These two read `isinstance(value, int)`, which is true for an event
        # handed straight over by the port and FALSE for the identical event read
        # back off a manifest: the manifest's network-event fields are typed
        # `dict[str, str]`, so `sequence` comes back as "1" and `timestamp_ms` as
        # "5983". The result was silent and total — on a real crawl's stored
        # evidence EVERY endpoint row carried
        # first_sequence=None last_sequence=None first_timestamp_ms=None,
        # measured on 68 live events across 7 endpoints from
        # vkpowerlife.136-85-106-73.sslip.io.
        #
        # It matters twice over: the endpoint ordering below keys on
        # `first_sequence` and so fell through to its 1<<30 fallback for every
        # row, and M2.4's generation reads this inventory, so a compiled spec
        # could not know when an endpoint was first observed.
        #
        # This is the SAME defect class the `request_body_keys` note thirty lines
        # down already records and fixes — "an event re-read from a written
        # manifest carries the flattened string … an inventory that looked
        # complete and had lost the API contract". Two fields were missed. A
        # fixture cannot catch either, because a fixture passes Python ints.
        sequence = _int_or_none(event.get("sequence"))
        if sequence is not None:
            if row["first_sequence"] is None:
                row["first_sequence"] = sequence
            row["last_sequence"] = sequence

        timestamp = _int_or_none(event.get("timestamp_ms"))
        if timestamp is not None:
            if row["first_timestamp_ms"] is None:
                row["first_timestamp_ms"] = timestamp
            row["last_timestamp_ms"] = timestamp

        rtype = str(event.get("resource_type") or "").strip()[:20]
        if rtype and rtype not in row["resource_types"]:
            row["resource_types"].append(rtype)

        # Body key names arrive in one of two shapes and BOTH must work: the
        # port hands over the structured ``request_body`` dict, while an event
        # re-read from a written manifest carries the flattened
        # ``request_body_keys`` string (the manifest field is typed
        # ``dict[str, str]``).  Reading only the first shape silently produced an
        # empty ``request_keys`` for anyone rebuilding the inventory from stored
        # evidence — an inventory that looked complete and had lost the API
        # contract.
        body = event.get("request_body")
        names: list[str] = []
        if isinstance(body, Mapping):
            names = [str(k) for k in (body.get("keys") or [])]
        if not names:
            flat = str(event.get("request_body_keys") or "")
            names = [part for part in (p.strip() for p in flat.split(",")) if part]
        for name in names:
            name = name[:80]
            if name and name not in row["request_keys"]:
                if len(row["request_keys"]) < MAX_KEYS_PER_ENDPOINT:
                    row["request_keys"].append(name)

        token = str(event.get("action_token") or "")
        label = str(event.get("action_label") or "").strip()
        verb = str(event.get("action_verb") or "").strip()
        if label or verb:
            entry = {"verb": verb, "label": label[:200], "action_token": token}
            if entry not in row["actions"] and len(row["actions"]) < MAX_ACTIONS_PER_ENDPOINT:
                row["actions"].append(entry)
        if token:
            pair = (key, token)
            per_action_counts[pair] = per_action_counts.get(pair, 0) + 1
            if per_action_counts[pair] > 1:
                row["retried"] = True

    endpoints = sorted(
        rows.values(),
        key=lambda r: (r["first_sequence"] if r["first_sequence"] is not None else 1 << 30,
                       r["method"], r["path_template"]),
    )
    return {
        "endpoints": endpoints,
        "endpoint_count": len(endpoints),
        "event_count": event_count,
        "truncated": truncated,
    }


def merge_inventories(inventories: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold per-visit inventories into one crawl-level inventory.

    Aggregation is associative on the same key, so a crawl-level inventory is the
    same object a single build over the concatenated stream would produce — which
    is what lets the crawler aggregate incrementally without holding every event
    of a long crawl in memory.
    """
    merged: dict[str, dict[str, Any]] = {}
    event_count = 0
    truncated = False
    for inv in inventories or ():
        if not isinstance(inv, Mapping):
            continue
        event_count += _int_or_zero(inv.get("event_count"))
        truncated = truncated or bool(inv.get("truncated"))
        for row in (inv.get("endpoints") or []):
            if not isinstance(row, Mapping):
                continue
            key = f"{row.get('method')} {row.get('host')}{row.get('path_template')}"
            existing = merged.get(key)
            if existing is None:
                if len(merged) >= MAX_ENDPOINTS:
                    truncated = True
                    continue
                merged[key] = {k: (dict(v) if isinstance(v, dict)
                                   else list(v) if isinstance(v, list) else v)
                               for k, v in row.items()}
                continue
            existing["observed_count"] += _int_or_zero(row.get("observed_count"))
            for status, count in (row.get("statuses") or {}).items():
                existing["statuses"][str(status)] = (
                    existing["statuses"].get(str(status), 0) + _int_or_zero(count))
            for field_name in ("status_classes", "resource_types", "request_keys"):
                for item in (row.get(field_name) or []):
                    if item not in existing[field_name]:
                        existing[field_name].append(item)
            for action in (row.get("actions") or []):
                if action not in existing["actions"] and \
                        len(existing["actions"]) < MAX_ACTIONS_PER_ENDPOINT:
                    existing["actions"].append(action)
            for flag in ("has_server_error", "retried", "rate_limited"):
                existing[flag] = bool(existing.get(flag)) or bool(row.get(flag))
            rank = {"none": 0, "cookie": 1, "api_key": 2, "authorization": 3,
                    "basic": 4, "bearer": 5}
            if rank.get(str(row.get("auth_pattern")), 0) > \
                    rank.get(str(existing.get("auth_pattern")), 0):
                existing["auth_pattern"] = row.get("auth_pattern")
            if not existing.get("response_shape"):
                existing["response_shape"] = row.get("response_shape") or ""
            for lo_field in ("first_sequence", "first_timestamp_ms"):
                incoming, current = row.get(lo_field), existing.get(lo_field)
                if isinstance(incoming, int) and (
                        not isinstance(current, int) or incoming < current):
                    existing[lo_field] = incoming
            for hi_field in ("last_sequence", "last_timestamp_ms"):
                incoming, current = row.get(hi_field), existing.get(hi_field)
                if isinstance(incoming, int) and (
                        not isinstance(current, int) or incoming > current):
                    existing[hi_field] = incoming

    endpoints = sorted(
        merged.values(),
        key=lambda r: (r.get("first_sequence") if isinstance(r.get("first_sequence"), int)
                       else 1 << 30, str(r.get("method")), str(r.get("path_template"))),
    )
    return {
        "endpoints": endpoints,
        "endpoint_count": len(endpoints),
        "event_count": event_count,
        "truncated": truncated,
    }


__all__ = ["MAX_ENDPOINTS", "build_inventory", "merge_inventories"]
