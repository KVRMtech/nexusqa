"""Email surface — Source + Surface via Amazon SES.

Inbound: SES delivers email to S3 + publishes an SNS notification; the
SNS handler in ``app/routes/email.py`` verifies the SNS signature,
extracts the SES event, and forwards the parsed message to the
orchestrator.

Outbound: ``SesOutboundClient`` sends a threaded HTML reply via the
SES ``SendEmail`` API.

DKIM / SPF / DMARC verification is performed by SES at receive time;
the parser reads the ``spfVerdict`` / ``dkimVerdict`` / ``dmarcVerdict``
fields and refuses processing on a ``FAIL``.
"""

from __future__ import annotations

from .composer import EmailComposer
from .dispatcher import EmailDispatcher, EmailDispatchError, SesOutboundClient
from .installation import (
    EmailInstallation,
    EmailInstallationError,
    EmailInstallationLoader,
)
from .parser import (
    EmailInboundError,
    ParsedEmail,
    SesVerdictFailure,
    parse_ses_email_event,
)
from .sns import (
    SnsSignatureError,
    SnsSubscriptionConfirmation,
    parse_sns_notification,
    verify_sns_signature,
)
from .handler import build_email_handler

__all__ = [
    "EmailComposer",
    "EmailDispatchError",
    "EmailDispatcher",
    "EmailInboundError",
    "EmailInstallation",
    "EmailInstallationError",
    "EmailInstallationLoader",
    "ParsedEmail",
    "SesOutboundClient",
    "SesVerdictFailure",
    "SnsSignatureError",
    "SnsSubscriptionConfirmation",
    "build_email_handler",
    "parse_ses_email_event",
    "parse_sns_notification",
    "verify_sns_signature",
]
