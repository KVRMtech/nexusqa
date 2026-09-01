"""Slack event payload parsing."""

from __future__ import annotations

from app.slack.events import SlackEventKind, parse_slack_event


def test_url_verification_returns_challenge() -> None:
    parsed = parse_slack_event(
        {"type": "url_verification", "challenge": "abc123"}
    )
    assert parsed.kind == SlackEventKind.URL_VERIFICATION
    assert parsed.challenge == "abc123"


def test_app_mention_recognised() -> None:
    parsed = parse_slack_event(
        {
            "type": "event_callback",
            "team_id": "T01",
            "event_id": "Ev01",
            "event": {
                "type": "app_mention",
                "user": "U001",
                "channel": "C001",
                "text": "<@U0BOT> what is LT5?",
                "thread_ts": "1620000000.0001",
                "event_ts": "1620000001.0001",
            },
        }
    )
    assert parsed.kind == SlackEventKind.APP_MENTION
    assert parsed.team_id == "T01"
    assert parsed.user_id == "U001"
    assert parsed.channel_id == "C001"
    assert parsed.thread_ts == "1620000000.0001"
    assert "LT5" in parsed.text


def test_message_im_recognised() -> None:
    parsed = parse_slack_event(
        {
            "type": "event_callback",
            "team_id": "T01",
            "event_id": "Ev02",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "U002",
                "channel": "D002",
                "text": "How does the tobacco rule work?",
            },
        }
    )
    assert parsed.kind == SlackEventKind.MESSAGE_IM
    assert parsed.user_id == "U002"
    assert parsed.channel_id == "D002"


def test_channel_message_recognised() -> None:
    parsed = parse_slack_event(
        {
            "type": "event_callback",
            "team_id": "T01",
            "event_id": "Ev03",
            "event": {
                "type": "message",
                "channel_type": "channel",
                "user": "U003",
                "channel": "C003",
                "text": "any quick question about the API?",
            },
        }
    )
    assert parsed.kind == SlackEventKind.MESSAGE_CHANNEL


def test_bot_message_filtered() -> None:
    parsed = parse_slack_event(
        {
            "type": "event_callback",
            "team_id": "T01",
            "event": {
                "type": "message",
                "channel_type": "channel",
                "bot_id": "B001",
                "text": "I am the bot",
            },
        }
    )
    assert parsed.kind == SlackEventKind.IGNORED


def test_message_changed_subtype_filtered() -> None:
    parsed = parse_slack_event(
        {
            "type": "event_callback",
            "team_id": "T01",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "text": "edited",
            },
        }
    )
    assert parsed.kind == SlackEventKind.IGNORED


def test_unknown_payload_type_ignored() -> None:
    parsed = parse_slack_event({"type": "something_else"})
    assert parsed.kind == SlackEventKind.IGNORED


def test_non_dict_payload_ignored() -> None:
    parsed = parse_slack_event(["nope"])  # type: ignore[arg-type]
    assert parsed.kind == SlackEventKind.IGNORED
