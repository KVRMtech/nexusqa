"""Slack integration: signature verification, event parsing, outbound client,
Block Kit composer, installation credential loader.
"""

from .composer import EchoCard, EchoCardComposer
from .events import ParsedSlackEvent, SlackEventKind, parse_slack_event
from .installation import (
    SlackInstallation,
    SlackInstallationError,
    SlackInstallationLoader,
)
from .interactions import (
    ParsedInteraction,
    parse_block_actions,
)
from .signature import (
    SlackSignatureInvalid,
    SlackSignatureMissing,
    SlackSignatureReplay,
    verify_slack_signature,
)
from .client import SlackClient, SlackClientError, SlackPostResult

__all__ = [
    "EchoCard",
    "EchoCardComposer",
    "ParsedInteraction",
    "ParsedSlackEvent",
    "SlackClient",
    "SlackClientError",
    "SlackEventKind",
    "SlackInstallation",
    "SlackInstallationError",
    "SlackInstallationLoader",
    "SlackPostResult",
    "SlackSignatureInvalid",
    "SlackSignatureMissing",
    "SlackSignatureReplay",
    "parse_block_actions",
    "parse_slack_event",
    "verify_slack_signature",
]
