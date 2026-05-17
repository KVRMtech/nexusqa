"""Audio canonicalization regression."""

from __future__ import annotations

import time

import pytest

from tests.regression.conftest import assert_matches_golden


AUDIO_FIXTURES = ["kt-call-en-01", "kt-call-es-01"]


@pytest.mark.parametrize("fixture_id", AUDIO_FIXTURES)
def test_audio_canonicalization(
    fixture_id: str,
    manifest,
    fixture_loader,
    orchestrator_client,
):
    fixture = manifest[fixture_id]
    media_path = fixture_loader(fixture_id)

    # 1. Submit
    with media_path.open("rb") as fh:
        resp = orchestrator_client.post(
            "/api/v1/canonical-workflows",
            json={
                "kind": fixture.kind,
                "tenant_id": "regression",
                "session_id": f"reg-{fixture_id}",
                "profile": fixture.profile,
                "initial_state": {"input_file": str(media_path)},
                "metadata": {"regression_id": fixture_id},
            },
        )
    assert resp.status_code in (200, 201), resp.text
    wf_id = resp.json()["workflow_id"]

    # 2. Poll until terminal
    deadline = time.time() + 900   # 15-minute Phase 1 SLO
    final = None
    while time.time() < deadline:
        st = orchestrator_client.get(f"/api/v1/canonical-workflows/{wf_id}")
        st.raise_for_status()
        body = st.json()
        if body["status"] in {"completed", "failed", "cancelled", "quarantined"}:
            final = body
            break
        time.sleep(5)
    assert final is not None, f"{fixture_id}: workflow did not terminate within SLO"
    assert final["status"] == "completed", (
        f"{fixture_id}: status={final['status']} step={final.get('current_step')} "
        f"err={final.get('error')}"
    )

    # 3. Pull the final canonical output via the orchestrator's
    #    history endpoint — the last completed step's payload carries
    #    a `summary` shape consumable by the golden comparator.
    hist = orchestrator_client.get(f"/api/v1/canonical-workflows/{wf_id}/history").json()
    last_completed = next(
        (h for h in reversed(hist["steps"]) if h["status"] == "completed"),
        None,
    )
    assert last_completed is not None, "no completed step in history"

    # 4. Compare against the golden output.
    summary = (last_completed.get("payload") or {}).get("summary") or {}
    assert_matches_golden(fixture_id, summary)
