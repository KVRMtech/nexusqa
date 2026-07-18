"""Seed-Manifest flow grouping — the fix for the confusing flat field list.

LIVE E2E on a bank app onboarded at /bank/transfer showed a flat list mixing login,
transfer and loan fields with identical copy. These lock in: auth is split off (and
satisfied by stored creds), the PRIMARY flow is the onboarded one, and progress reflects
already-provided values. Grouping degrades to one honest bucket when nothing matches.
"""
from app.services.flow_grouping import group_into_flows

# Mirrors the real bb03329f manifest (labels + dispositions).
ITEMS = [
    {"label": "Username", "disposition": "ASK"},
    {"label": "Password", "disposition": "ASK"},
    {"label": "Remember me", "disposition": "ASK"},
    {"label": "From Account", "disposition": "ASK"},
    {"label": "Payee", "disposition": "ASK"},
    {"label": "Account", "disposition": "ASK"},
    {"label": "Loan Type", "disposition": "ASK"},
    {"label": "Amount", "disposition": "SYNTHESIZE"},
]


def test_transfer_is_primary_from_entry_url():
    g = group_into_flows(ITEMS, base_url="https://x.test/bank/transfer")
    assert g["primary_flow"] == "transfer"
    assert g["flows"][0]["key"] == "transfer" and g["flows"][0]["primary"] is True
    # Loan is present but not primary.
    keys = [f["key"] for f in g["flows"]]
    assert "loan" in keys and keys.index("transfer") < keys.index("loan")


def test_auth_split_and_satisfied_by_stored_creds():
    g = group_into_flows(ITEMS, base_url="https://x.test/bank/transfer", auth_satisfied=True)
    assert g["auth"] is not None
    assert g["auth"]["satisfied"] is True
    assert g["auth"]["to_provide"] == 0  # stored creds satisfy the whole login group
    auth_labels = {i["label"] for i in g["auth"]["items"]}
    assert {"Username", "Password", "Remember me"} <= auth_labels
    # Auth fields must NOT leak into the data flows.
    flow_labels = {i["label"] for f in g["flows"] for i in f["items"]}
    assert not ({"Username", "Password"} & flow_labels)


def test_progress_counts_provided_values():
    g = group_into_flows(
        ITEMS, base_url="https://x.test/bank/transfer", provided_labels=["From Account"],
    )
    transfer = next(f for f in g["flows"] if f["key"] == "transfer")
    provided = {i["label"] for i in transfer["items"] if i["provided"]}
    assert "From Account" in provided
    # transfer has 3 actionable ASK fields (From Account, Payee, Account); one provided.
    assert transfer["actionable"] == 3 and transfer["to_provide"] == 2
    # Auto-handled Amount is in the flow (total 4) but never counted as "to provide".
    assert transfer["total"] == 4
    amount = next(i for i in transfer["items"] if i["label"] == "Amount")
    assert amount["disposition"] == "SYNTHESIZE"


def test_page_url_grounds_flow_generically_without_domain_keywords():
    # No banking vocabulary at all — grouping comes purely from the pages the crawler saw.
    items = [
        {"label": "Widget Serial", "disposition": "ASK"},
        {"label": "Batch Code", "disposition": "ASK"},
        {"label": "Ship To", "disposition": "ASK"},
    ]
    urls = {
        "Widget Serial": "https://f.test/intake/register",
        "Batch Code": "https://f.test/intake/register",
        "Ship To": "https://f.test/orders/dispatch",
    }
    g = group_into_flows(items, base_url="https://f.test/intake/register", field_urls=urls)
    keys = [f["key"] for f in g["flows"]]
    assert "register" in keys and "dispatch" in keys
    reg = next(f for f in g["flows"] if f["key"] == "register")
    assert {i["label"] for i in reg["items"]} == {"Widget Serial", "Batch Code"}
    assert reg["primary"] is True and reg["name"] == "Register"  # base_url points here


def test_page_honest_grouping_never_merges_across_pages():
    # The live defect: "From Account"/"Payee" (captured on /bank/send-money) were merged
    # into a fabricated "Transfer" flow. Page is ground truth — fields from different
    # pages must land in different flows, never a keyword-stitched fiction.
    items = [
        {"label": "From Account", "disposition": "PICK", "uncaptured_options": True},
        {"label": "Payee", "disposition": "PICK", "uncaptured_options": True},
        {"label": "Account", "disposition": "PICK", "uncaptured_options": True},
    ]
    urls = {
        "From Account": "https://x.test/bank/send-money",
        "Payee": "https://x.test/bank/send-money",
        "Account": "https://x.test/bank/transactions",
    }
    g = group_into_flows(items, base_url="https://x.test/bank/transfer", field_urls=urls)
    keys = {f["key"] for f in g["flows"]}
    assert "send money" in keys and "transactions" in keys
    assert "transfer" not in keys  # no fabricated Transfer flow
    sm = next(f for f in g["flows"] if f["key"] == "send money")
    assert {i["label"] for i in sm["items"]} == {"From Account", "Payee"}


def test_missing_primary_flagged_when_entry_page_not_captured():
    # base_url is /bank/transfer but only Send Money was captured — say so honestly.
    items = [{"label": "Payee", "disposition": "PICK", "uncaptured_options": True}]
    urls = {"Payee": "https://x.test/bank/send-money"}
    g = group_into_flows(items, base_url="https://x.test/bank/transfer", field_urls=urls)
    assert g["missing_primary"] is not None
    assert g["missing_primary"]["key"] == "transfer"
    assert "re-crawl" in g["missing_primary"]["reason"].lower()


def test_missing_primary_uses_captured_routes_not_a_hint_bucket():
    # "Amount" (auto-filled, no page URL) hint-creates a "transfer" bucket, but the real
    # /bank/transfer page was never crawled. captured_paths is ground truth — so
    # missing_primary must STILL fire, and the hint bucket must NOT be marked primary.
    items = [
        {"label": "Amount", "disposition": "SYNTHESIZE"},
        {"label": "Payee", "disposition": "PICK", "uncaptured_options": True},
    ]
    urls = {"Payee": "https://x.test/bank/send-money"}  # Amount carries no URL
    g = group_into_flows(
        items, base_url="https://x.test/bank/transfer", field_urls=urls,
        captured_paths=["/bank/login", "/bank/send-money"],
    )
    assert g["missing_primary"] is not None and g["missing_primary"]["key"] == "transfer"
    assert g["primary_flow"] is None


def test_uncaptured_dropdown_flow_never_reads_ready():
    # A flow made only of unread dropdowns has 0 actionable — it must carry uncaptured>0
    # so the UI cannot render it "0 of 0 ready" (a false green).
    items = [{"label": "From Account", "disposition": "PICK", "uncaptured_options": True}]
    urls = {"From Account": "https://x.test/bank/send-money"}
    g = group_into_flows(items, base_url="https://x.test/bank/send-money", field_urls=urls)
    flow = next(f for f in g["flows"] if f["key"] == "send money")
    assert flow["actionable"] == 0 and flow["uncaptured"] == 1


def test_ui_action_controls_are_dropped_from_data_flows():
    # The practice app's "Mark TC-DASH-001 as done" checkboxes and an "Enable Two-Factor"
    # toggle are UI actions, not values to provide — they must never appear as ASK rows.
    items = [
        {"label": "From Account", "disposition": "ASK"},
        {"label": "Mark TC-DASH-001 as done", "disposition": "ASK"},
        {"label": "Enable Two-Factor Authentication", "disposition": "ASK"},
    ]
    g = group_into_flows(items, base_url="https://x.test/bank/transfer")
    labels = {i["label"] for f in g["flows"] for i in f["items"]}
    assert "From Account" in labels
    assert "Mark TC-DASH-001 as done" not in labels
    assert "Enable Two-Factor Authentication" not in labels


def test_non_domain_app_degrades_to_one_honest_bucket():
    items = [{"label": "Widget Serial", "disposition": "ASK"}, {"label": "Batch Code", "disposition": "ASK"}]
    g = group_into_flows(items, base_url="https://factory.test/intake")
    assert len(g["flows"]) == 1
    assert g["flows"][0]["key"] == "other" and g["flows"][0]["name"] == "Fields to provide"
    assert g["auth"] is None
