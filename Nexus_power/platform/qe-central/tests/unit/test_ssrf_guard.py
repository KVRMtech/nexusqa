"""SSRF guard for the tier-4 auth-hook URL (_is_safe_public_hook).

Deterministic cases only — scheme + literal-host + IP-literal checks need no DNS.
Blocks non-https, loopback/metadata/internal hosts, and private/link-local IPs so
an operator-configured hook can't be pointed at internal services or the cloud
metadata endpoint.
"""
from __future__ import annotations

from app.routers.explorations import _is_safe_public_hook


def test_rejects_non_https():
    assert _is_safe_public_hook("http://example.com/s")[0] is False
    assert _is_safe_public_hook("ftp://example.com/s")[0] is False
    assert _is_safe_public_hook("file:///etc/passwd")[0] is False


def test_rejects_internal_host_literals():
    for u in (
        "https://localhost/s",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://vault.corp.internal/s",
        "https://db.local/s",
    ):
        assert _is_safe_public_hook(u)[0] is False, u


def test_rejects_private_loopback_and_metadata_ip_literals():
    for u in (
        "https://127.0.0.1/s",
        "https://10.0.0.5/s",
        "https://192.168.1.1/s",
        "https://172.16.0.1/s",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "https://[::1]/s",
        "https://0.0.0.0/s",
    ):
        assert _is_safe_public_hook(u)[0] is False, u


def test_accepts_public_ip_literal():
    ok, reason = _is_safe_public_hook("https://8.8.8.8/session")
    assert ok is True, reason


def test_rejects_empty_or_garbage():
    assert _is_safe_public_hook("")[0] is False
    assert _is_safe_public_hook("https://")[0] is False
