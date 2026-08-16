"""R5 — HOST ALLOWLIST BYPASS (SSRF via a tenant-controlled egress fence).

ATTACK
======
``fences.allowed_hosts`` is tenant-supplied and becomes the squid egress
allowlist verbatim.  Squid reads ``.com`` as "this domain and every subdomain",
so registering ``[".com"]`` converted the fenced browser into an open proxy for
the entire ``.com`` namespace.  Registering ``169.254.169.254`` points it at the
cloud metadata service; ``[::1]`` and ``0x7f000001`` are the same attack wearing
a different encoding.

Nothing validated any of it at write time — the value was persisted first and
only partially sanity-checked at crawl time.

EXPECTED
========
Refused at the WRITE boundary, in every encoding, with the offending entry and
the reason named so an operator can fix it.
"""
from __future__ import annotations

import pytest

from app.security.host_policy import (
    HostPolicyError,
    validate_allowed_hosts,
    validate_host_entry,
)

# ── the catastrophic forms named in the brief ──────────────────────────────

PUBLIC_SUFFIX_FORMS = [
    ".com", "*.com", "com", ".co.uk", "*.co.uk", "co.uk", ".net", "*.org",
    ".io", "*.dev", ".app",
]

WILDCARD_FORMS = ["*", "*.", "**", "*.*", ".", "..", "*.%2E"]

METADATA_AND_INTERNAL = [
    "169.254.169.254",            # AWS/GCP/Azure IMDS
    "metadata.google.internal",
    "metadata",
    "metadata.goog",
    "instance-data",
    "100.100.100.200",            # Alibaba metadata
    "fd00:ec2::254",              # AWS IPv6 IMDS
]

PRIVATE_AND_LOOPBACK = [
    "127.0.0.1", "localhost", "0.0.0.0", "10.0.0.5", "192.168.1.1",
    "172.16.4.9", "169.254.1.1", "::1", "[::1]", "fe80::1", "fc00::1",
    "::ffff:169.254.169.254",     # IPv4-mapped IPv6 metadata
    "::ffff:127.0.0.1",
]

ENCODED_AND_ALTERNATE = [
    "2130706433",                 # 127.0.0.1 as a bare integer
    "0x7f000001",                 # hex
    "0177.0.0.1",                 # octal
    "127.1",                      # short form
    "0x7f.1",
    "%6C%6F%63%61%6C%68%6F%73%74",       # percent-encoded "localhost"
    "%2E%63%6F%6D",                      # percent-encoded ".com"
    "%252E%63%6F%6D",                    # double-encoded ".com"
    "LOCALHOST",
    "localhost.",                        # trailing root dot
    "http://169.254.169.254/latest/",    # pasted URL
    "https://acme.example/admin",        # a URL that LOOKS scoped but is not
    "acme.example/path",                 # bare host + path — same illusion
    "user:pw@169.254.169.254",           # userinfo
    "169.254.169.254:80",                # with a port
    "local​host",                   # zero-width space inside "localhost"
    "local﻿host",                   # BOM inside "localhost"
    "localhost­",                   # soft hyphen
]

MALFORMED = [
    "", "   ", "-bad.example", "bad-.example", "exa mple.com", "a..b.example",
    "under_score.example", "x" * 300, "acme.example/path", "10.0.0.0/8",
    "acme.example\\evil",
]

INTERNAL_NAMESPACES = [
    "svc.cluster.local", "app.svc.cluster.local", "printer.local",
    "db.internal", "host.intranet", "thing.corp", "x.lan", "y.private",
    "qe-central", "platform-api", "postgres",
]

MULTI_TENANT_HOSTS = [
    "herokuapp.com", ".herokuapp.com", "*.appspot.com", "s3.amazonaws.com",
    "github.io", ".vercel.app", "sslip.io", "nip.io",
]


@pytest.mark.parametrize("entry", (
    PUBLIC_SUFFIX_FORMS + WILDCARD_FORMS + METADATA_AND_INTERNAL
    + PRIVATE_AND_LOOPBACK + ENCODED_AND_ALTERNATE + MALFORMED
    + INTERNAL_NAMESPACES + MULTI_TENANT_HOSTS
))
def test_r5_every_dangerous_form_is_refused(entry):
    with pytest.raises(HostPolicyError):
        validate_host_entry(entry)


def test_r5_the_headline_case_from_the_brief():
    """Registration containing ``[".com"]`` MUST fail."""
    with pytest.raises(HostPolicyError) as exc:
        validate_allowed_hosts([".com"])
    assert ".com" in exc.value.entry
    assert "public suffix" in exc.value.reason


def test_r5_one_bad_entry_refuses_the_WHOLE_list():
    """A partially-accepted allowlist is not a fence.

    Silently dropping the bad entry would leave the operator believing they had
    configured something they had not."""
    with pytest.raises(HostPolicyError):
        validate_allowed_hosts(["acme.example", ".com", "other.example"])


def test_r5_a_list_cannot_be_a_bare_string():
    with pytest.raises(HostPolicyError):
        validate_allowed_hosts(".com")


def test_r5_the_list_is_length_bounded():
    with pytest.raises(HostPolicyError):
        validate_allowed_hosts([f"h{i}.example" for i in range(500)])


# ── the positive half: legitimate fences still work ────────────────────────

@pytest.mark.parametrize("entry,expected", [
    ("acme.example", "acme.example"),
    ("ACME.Example", "acme.example"),
    ("acme.example.", "acme.example"),
    (".acme-life.example", ".acme-life.example"),
    ("*.acme-life.example", ".acme-life.example"),
    ("okta.com", "okta.com"),
    ("login.microsoftonline.com", "login.microsoftonline.com"),
    ("summitlife-admin.136-85-106-73.sslip.io",
     "summitlife-admin.136-85-106-73.sslip.io"),
    ("acme-life", "acme-life"),          # a container/service name on an internal net
    ("app.acme.example:8443", "app.acme.example"),   # a port is unambiguous
])
def test_r5_legitimate_entries_are_accepted_and_normalised(entry, expected):
    assert validate_host_entry(entry) == expected


def test_r5_normalisation_happens_before_validation_not_after():
    """The ordering IS the control.

    ``%2E%63%6F%6D`` only looks safe until it is decoded; a checker that
    validated first and normalised second would pass it straight through."""
    with pytest.raises(HostPolicyError):
        validate_host_entry("%2E%63%6F%6D")
    # …and the same bytes, already decoded, are refused identically.
    with pytest.raises(HostPolicyError):
        validate_host_entry(".com")


def test_r5_duplicates_collapse_to_the_canonical_form():
    assert validate_allowed_hosts(
        ["ACME.example", "acme.example.", "acme.example"]) == ["acme.example"]


# ── the write boundary, not just the helper ────────────────────────────────

def test_r5_the_write_gate_refuses_with_an_actionable_422():
    """T-SEC-04: validation lives where the value is PERSISTED."""
    from fastapi import HTTPException

    from app.routers.apps import _validated_fences

    with pytest.raises(HTTPException) as exc:
        _validated_fences({"allowed_hosts": [".com"]})
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert detail["field"] == "fences.allowed_hosts"
    assert detail["entry"] == ".com"
    assert "public suffix" in detail["reason"]


def test_r5_idp_domains_are_fenced_too():
    """The SSO allowlist is appended to the egress fence, so it is the same door."""
    from fastapi import HTTPException

    from app.routers.apps import _validated_fences

    with pytest.raises(HTTPException):
        _validated_fences({"idp_domains": ["*.com"]})


def test_r5_the_write_gate_stores_the_normalised_form():
    """What is stored must be what was checked."""
    from app.routers.apps import _validated_fences

    out = _validated_fences({"allowed_hosts": ["ACME.Example.", "*.acme.example"]})
    assert out["allowed_hosts"] == ["acme.example", ".acme.example"]


def test_r5_dispatch_re_validates_rows_written_before_the_gate_existed():
    """Defence in depth: an already-persisted unsafe fence still cannot reach squid."""
    from fastapi import HTTPException

    from app.routers.explorations import _allowlist_domains

    with pytest.raises(HTTPException) as exc:
        _allowlist_domains("https://acme.example", {"allowed_hosts": [".com"]})
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "unsafe_egress_allowlist"


def test_r5_dispatch_still_resolves_a_legitimate_fence():
    from app.routers.explorations import _allowlist_domains

    assert _allowlist_domains(
        "https://app.acme.example/quote",
        {"allowed_hosts": [".acme.example"], "idp_domains": ["okta.com"]},
    ) == [".acme.example", "okta.com"]


def test_r5_dispatch_falls_back_to_the_base_url_host():
    from app.routers.explorations import _allowlist_domains

    assert _allowlist_domains("https://app.acme.example/quote", {}) == [
        "app.acme.example"]
