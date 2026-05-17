"""Slack interactivity (block_actions) parser."""

from __future__ import annotations

import json

from app.slack.interactions import parse_block_actions


def test_block_actions_thumbs_up_parsed() -> None:
    payload = {
        "type": "block_actions",
        "team": {"id": "T01"},
        "user": {"id": "U001"},
        "channel": {"id": "C001"},
        "message": {"ts": "1620000000.0001"},
        "response_url": "https://hooks.slack.com/actions/...",
        "actions": [
            {
                "action_id": "echo_feedback:thumbs_up",
                "value": "dispatch-xyz",
                "block_id": "echo_feedback:dispatch-xyz",
            }
        ],
    }
    parsed = parse_block_actions(json.dumps(payload))
    assert parsed.kind == "block_actions"
    assert parsed.team_id == "T01"
    assert parsed.user_id == "U001"
    assert parsed.channel_id == "C001"
    assert parsed.action_id == "echo_feedback:thumbs_up"
    assert parsed.action_value == "dispatch-xyz"


def test_unsupported_type_ignored() -> None:
    payload = {"type": "shortcut"}
    parsed = parse_block_actions(json.dumps(payload))
    assert parsed.kind == "ignored"


def test_missing_actions_ignored() -> None:
    payload = {"type": "block_actions", "actions": []}
    parsed = parse_block_actions(json.dumps(payload))
    assert parsed.kind == "ignored"


def test_invalid_json_ignored() -> None:
    parsed = parse_block_actions("not-json")
    assert parsed.kind == "ignored"


def test_empty_input_ignored() -> None:
    parsed = parse_block_actions("")
    assert parsed.kind == "ignored"
