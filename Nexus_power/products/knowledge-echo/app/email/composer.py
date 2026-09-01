"""HTML email composer.

Renders a small, self-contained HTML body (inlined CSS so it survives
Gmail/Outlook). The composer returns three artifacts inside the
``ComposedPayload.payload`` dict:

    {
      "subject":     "Re: <original subject>",
      "html_body":   "<!doctype html>...",
      "text_body":   "plain-text fallback",
      "in_reply_to": "<original Message-Id>",
      "references":  ["<id1>", "<id2>", ...]
    }

The dispatcher consumes the dict directly and hands it to SES.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from ..matcher import MatchResult
from ..surfaces import ComposedPayload


@dataclass(frozen=True)
class EmailComposerContext:
    """Optional context the route may attach to the composer call.

    ``in_reply_to`` and ``references`` keep email clients threading the
    echo with the original question. ``original_subject`` is used to
    build the ``Re:`` reply subject.
    """

    in_reply_to: Optional[str] = None
    references: tuple[str, ...] = ()
    original_subject: Optional[str] = None
    asker_name: Optional[str] = None


class EmailComposer:
    """Implements ``SurfaceComposer`` for the email surface.

    The composer can be passed a context via ``with_context()`` so the
    route can supply the original Message-ID / Subject without
    extending the orchestrator's API.
    """

    def __init__(self) -> None:
        self._ctx_stack: list[EmailComposerContext] = []

    def with_context(
        self, ctx: EmailComposerContext
    ) -> "_ScopedEmailComposer":
        return _ScopedEmailComposer(self, ctx)

    def compose(
        self,
        *,
        dispatch_id: str,
        question_text: str,
        match: MatchResult,
    ) -> Optional[ComposedPayload]:
        return self._compose(
            dispatch_id=dispatch_id,
            question_text=question_text,
            match=match,
            ctx=EmailComposerContext(),
        )

    # ── Internals ───────────────────────────────────────────────

    def _compose(
        self,
        *,
        dispatch_id: str,
        question_text: str,
        match: MatchResult,
        ctx: EmailComposerContext,
    ) -> Optional[ComposedPayload]:
        if match.is_empty:
            return None
        top = match.candidates[0]
        similarity_pct = int(
            round(max(0.0, min(1.0, top.similarity)) * 100)
        )
        subject = _reply_subject(ctx.original_subject)
        text_body = _text_body(
            question_text=question_text,
            similarity_pct=similarity_pct,
            primary=top,
            asker_name=ctx.asker_name,
        )
        html_body = _html_body(
            question_text=question_text,
            similarity_pct=similarity_pct,
            primary=top,
            asker_name=ctx.asker_name,
            dispatch_id=dispatch_id,
        )
        payload: dict[str, Any] = {
            "subject": subject,
            "html_body": html_body,
            "text_body": text_body,
            "in_reply_to": ctx.in_reply_to,
            "references": list(ctx.references),
            "dispatch_id": dispatch_id,
        }
        return ComposedPayload(
            surface="email",
            text=text_body,
            payload=payload,
            payload_hash=_hash_payload(payload),
            similarity_pct=similarity_pct,
            primary_candidate=top.to_audit_dict(),
        )


class _ScopedEmailComposer:
    """Adapter that satisfies ``SurfaceComposer`` with a frozen context."""

    def __init__(self, parent: EmailComposer, ctx: EmailComposerContext):
        self._parent = parent
        self._ctx = ctx

    def compose(
        self,
        *,
        dispatch_id: str,
        question_text: str,
        match: MatchResult,
    ) -> Optional[ComposedPayload]:
        return self._parent._compose(  # noqa: SLF001
            dispatch_id=dispatch_id,
            question_text=question_text,
            match=match,
            ctx=self._ctx,
        )


# ── Helpers ─────────────────────────────────────────────────────


def _reply_subject(original: Optional[str]) -> str:
    if not original:
        return "Knowledge Echo"
    stripped = original.strip()
    if stripped.lower().startswith("re:"):
        return stripped[:200]
    return f"Re: {stripped}"[:200]


def _text_body(
    *,
    question_text: str,
    similarity_pct: int,
    primary,
    asker_name: Optional[str],
) -> str:
    greeting = (
        f"Hi {asker_name}," if asker_name else "Hi,"
    )
    sme = []
    if primary.speaker_id:
        sme.append(primary.speaker_id)
    if primary.speaker_role:
        sme.append(f"({primary.speaker_role})")
    sme_line = " ".join(sme) or "an internal subject-matter source"
    when = f"session {primary.session_id}" if primary.session_id else ""
    return (
        f"{greeting}\n\n"
        f"Knowledge Echo — {similarity_pct}% match\n\n"
        f"You asked: {question_text}\n\n"
        f"Closest prior answer (from {sme_line} {when}):\n\n"
        f"  \"{primary.text}\"\n\n"
        "If this helped, just reply 'yes'. If it didn't, reply 'no' "
        "and we'll route you to the subject-matter expert.\n\n"
        "— Nexus Knowledge Echo"
    )


_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{subject_esc}</title>
</head>
<body style="margin:0;padding:0;background:#f7f7f9;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#111;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td align="center" style="padding:24px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;">
        <tr><td style="padding:20px 24px;border-bottom:1px solid #f1f1f4;">
          <div style="font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.04em;">Knowledge Echo</div>
          <div style="font-size:22px;font-weight:600;margin-top:4px;">{sim}% match</div>
        </td></tr>
        <tr><td style="padding:20px 24px;">
          <div style="font-size:14px;color:#374151;margin-bottom:8px;">You asked:</div>
          <div style="font-size:15px;line-height:1.5;color:#111827;background:#f9fafb;border-radius:6px;padding:12px 14px;border:1px solid #e5e7eb;">
            {question_esc}
          </div>
          <div style="font-size:14px;color:#374151;margin:20px 0 8px 0;">Closest prior answer{sme_attr}:</div>
          <blockquote style="margin:0;padding:14px 16px;border-left:3px solid #6366f1;background:#eef2ff;font-size:15px;line-height:1.55;color:#1e1b4b;border-radius:0 6px 6px 0;">
            {primary_esc}
          </blockquote>
          <div style="font-size:13px;color:#6b7280;margin-top:18px;">
            Reply <strong>yes</strong> if this helped &mdash; reply <strong>no</strong> if it missed and we'll route you to the SME.
          </div>
        </td></tr>
        <tr><td style="padding:14px 24px;border-top:1px solid #f1f1f4;background:#fafafa;font-size:12px;color:#9ca3af;">
          dispatch {dispatch_id_esc}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""


def _html_body(
    *,
    question_text: str,
    similarity_pct: int,
    primary,
    asker_name: Optional[str],
    dispatch_id: str,
) -> str:
    sme_bits = []
    if primary.speaker_id:
        sme_bits.append(html.escape(primary.speaker_id))
    if primary.speaker_role:
        sme_bits.append(f"({html.escape(primary.speaker_role)})")
    if primary.session_id:
        sme_bits.append(f"session {html.escape(primary.session_id)}")
    sme_attr = (" from " + " ".join(sme_bits)) if sme_bits else ""
    return _HTML_TEMPLATE.format(
        subject_esc=html.escape(_reply_subject(None)),
        sim=similarity_pct,
        question_esc=html.escape(question_text),
        primary_esc=html.escape(primary.text),
        sme_attr=sme_attr,
        dispatch_id_esc=html.escape(dispatch_id),
    )


def _hash_payload(payload: dict[str, Any]) -> str:
    body = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()
