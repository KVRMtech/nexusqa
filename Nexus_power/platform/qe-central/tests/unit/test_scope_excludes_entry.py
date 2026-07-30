"""A crawl whose scope excludes its own entry point must be REFUSED, not "completed".

The founder hit this: Base URL entered at '/', Target mode confined to
'/portal/apply'. The crawler logged `out_of_scope depth=0` and finished with
states=0, actions=0 — and reported stop_reason=completed. The UI then advised
checking whether the URL was reachable and public, which it was. A configuration
that cannot capture anything must fail up front and name both values.

Pure — mirrors the dispatch guard's predicate.
"""
from urllib.parse import urlparse


def _entry_in_scope(base_url: str, scope_paths: list[str]) -> bool:
    """The dispatch guard's rule: with no scope everything is in scope; otherwise
    the entry path must sit inside a scope prefix, or be a parent of one."""
    if not scope_paths:
        return True
    entry = urlparse(base_url).path or "/"
    return any(entry.startswith(sp) for sp in scope_paths)


def test_the_founders_exact_configuration_is_refused():
    assert _entry_in_scope("https://app.example.com", ["/portal/apply"]) is False
    assert _entry_in_scope("https://app.example.com/", ["/portal/apply"]) is False


def test_entering_inside_the_scope_is_allowed():
    assert _entry_in_scope("https://app.example.com/portal/apply", ["/portal/apply"])
    assert _entry_in_scope("https://app.example.com/portal/apply/step2", ["/portal/apply"])


def test_entering_at_a_PARENT_of_the_scope_is_ALSO_refused():
    """Tempting to allow — the crawler could in principle walk down into scope. It
    does not: an out-of-scope URL is skipped at depth 0 (observed `out_of_scope
    depth=0` then states=0), so a parent entry captures nothing either. Allowing
    parents would also make '/' a parent of every scope and void the guard, which is
    exactly the bug this test caught in the first version."""
    assert _entry_in_scope("https://app.example.com/portal", ["/portal/apply"]) is False
    assert _entry_in_scope("https://app.example.com/", ["/portal/apply"]) is False


def test_any_one_matching_scope_is_enough():
    assert _entry_in_scope("https://app.example.com/quote", ["/portal", "/quote"])


def test_no_scope_means_explore_mode_and_is_never_refused():
    for base in ("https://app.example.com", "https://app.example.com/anything"):
        assert _entry_in_scope(base, []) is True


def test_a_sibling_path_is_still_refused():
    """'/portal/apply' must not be satisfied by '/portal-admin'."""
    assert _entry_in_scope("https://app.example.com/portal-admin", ["/portal/apply"]) is False
