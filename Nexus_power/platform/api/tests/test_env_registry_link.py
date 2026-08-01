"""One environment registry, seen from the runner side.

F6. Onboarding (qe-central `app_environments`) collects the rich Environment
Profile — base_url, routing cookies/headers, an env assertion — while the runner's
governance registry (`tp_environments`) held only posture/production/base_url/epoch.
Two lists, nothing linking them, and an operator reasonably believes they are one.

The harm was NOT cosmetic. environment_routing copies cookies, headers and the env
assertion out of the environment row into the run context, and the row had nowhere
to hold them — so a cookie-selected lane on a shared host silently landed on the
host's default, which for these estates is production. The copy could never fire.

Ownership does not move: qe-central still owns the profile and mirrors only the
NON-SECRET routing the runner must apply.
"""
import pytest

from app.services.test_factory import environment_routing, persona_store

_STORE = open("app/services/test_factory/persona_store.py", encoding="utf-8").read()
_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()
_SQL = open("scripts/apply_env_registry_link.sql", encoding="utf-8").read()


# ── the row can now hold what the run must apply ─────────────────────────────

def test_the_environment_row_has_somewhere_to_keep_its_routing():
    """environment_routing has always copied these; the row never had them."""
    for col in ("cookies", "headers", "env_assertion"):
        assert f"{col}: Mapped" in _STORE, col
        assert f"ADD COLUMN IF NOT EXISTS {col}" in _SQL, col


def test_the_row_records_WHICH_registry_it_came_from():
    for col in ("source", "app_env_id"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in _SQL, col
    assert '"source": getattr(r, "source", "") or "studio"' in _STORE


def test_the_migration_is_additive_and_idempotent():
    assert _SQL.count("ADD COLUMN IF NOT EXISTS") == 5
    assert "DROP" not in _SQL.upper()


def test_the_migration_is_actually_applied_by_something():
    """apply_card_contract.sql shipped referenced by nothing and had to be run by
    hand; an unapplied additive migration is not a quiet degradation, it is a 500
    on every write of a column the ORM already knows about."""
    runner = open("scripts/apply_all.sh", encoding="utf-8").read()
    assert "apply_env_registry_link.sql" in runner


# ── the routing now actually reaches the run ─────────────────────────────────

def test_routing_stored_on_the_environment_travels_into_the_run_context():
    """THE POINT. A cookie-selected lane on a shared host: dropping the cookie lands
    the run on the host's default, which for these estates is production."""
    env = {"environment_id": "uat", "base_url": "https://shared.example.com",
           "posture": "read_write",
           "cookies": [{"name": "x-env", "value": "uat"}],
           "headers": {"X-Lane": "uat"},
           "env_assertion": {"url_pattern": "/uat/"}}
    ctx = environment_routing.resolve_destination(
        environment_id="uat", environment=env)["env_context"]
    assert ctx["cookies"] == [{"name": "x-env", "value": "uat"}]
    assert ctx["headers"] == {"X-Lane": "uat"}
    assert ctx["env_assertion"] == {"url_pattern": "/uat/"}


# ── an edit from one screen must not blank the other's work ──────────────────

def test_a_governance_only_edit_does_not_blank_routing_pushed_by_onboarding():
    """Studio's form has no cookie fields. If omitting them cleared the columns,
    saving a posture would silently drop the lane selector and send the next run to
    the shared host's default."""
    seg = _STORE[_STORE.index("async def save_environment("):]
    seg = seg[:seg.index("# ── Scoped certification")]
    assert "if cookies is not None:" in seg
    assert "if headers is not None:" in seg
    assert "if env_assertion is not None:" in seg
    # …and the PUT body defaults them to None rather than to empty containers
    body = _ROUTER[_ROUTER.index("class _EnvironmentBody"):]
    body = body[:body.index("@router.put")]
    assert "cookies: list[dict] | None = Field(None" in body
    assert "headers: dict[str, str] | None = Field(None" in body


# ── ownership and secrecy are preserved ──────────────────────────────────────

def test_onboarding_mirrors_routing_but_never_asserts_POSTURE():
    """Posture is a governance decision on the runner side. Overwriting it from
    onboarding would silently re-open a production environment somebody locked."""
    client = open("../qe-central/app/clients/platform_api.py", encoding="utf-8").read()
    seg = client[client.index("async def mirror_environment_profile("):]
    assert '"source": "onboarding"' in seg
    assert '"posture"' not in seg.split("try:")[0]
    assert '"is_production"' not in seg.split("try:")[0]
    assert '"write_authorized"' not in seg.split("try:")[0]


def test_no_sealed_credential_is_ever_mirrored():
    """Only what the runner must APPLY travels. The profile's sealed creds_blob is
    decrypted solely inside qe-central's tenant-scoped session and must never appear
    in the payload."""
    client = open("../qe-central/app/clients/platform_api.py", encoding="utf-8").read()
    seg = client[client.index("async def mirror_environment_profile("):]
    payload = seg[seg.index("body = {"):seg.index("try:")]
    # Comments legitimately NAME creds_blob to say it stays behind; what must not
    # appear is a read of it, so judge the code and not the prose.
    code = "\n".join(l.split("#", 1)[0] for l in payload.splitlines())
    for forbidden in ("creds_blob", "password", "secret", "basic_auth", "decrypt"):
        assert forbidden not in code, forbidden
    assert "SECRETS ARE NOT MIRRORED" in seg


def test_the_mirror_never_fails_a_crawl_that_already_produced_an_artifact():
    client = open("../qe-central/app/clients/platform_api.py", encoding="utf-8").read()
    seg = client[client.index("async def mirror_environment_profile("):]
    assert "NEVER raises" in seg
    assert "except Exception as exc:" in seg
    internal = open("../qe-central/app/routers/internal.py", encoding="utf-8").read()
    m = internal[internal.index("mirror_environment_profile("):]
    assert "except Exception as exc:" in m[:900]


def test_the_mirror_runs_on_crawl_promotion_alongside_the_recipe():
    internal = open("../qe-central/app/routers/internal.py", encoding="utf-8").read()
    assert "ClientAppEnvironmentRow" in internal
    assert "platform_api.mirror_environment_profile(" in internal
    # after the promote transaction commits — never hold a DB transaction open
    # across an HTTP call
    assert internal.index("materialise_login_recipe(") < internal.index(
        "mirror_environment_profile(")
