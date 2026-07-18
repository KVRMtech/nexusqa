"""Semantic, locale-tolerant DESTRUCTIVE-INTENT classifier — the safety gate for any control
the crawler might ACTUATE (ACT-THEN-DIFF driver acts, reveal probes).

The base danger gate keys on an English verb list, so a non-English (or icon-only, or
unusually-labelled) *Delete / Pay / Deactivate* can slip through as "safe" and get clicked.
This adds two things the acting engine consults BEFORE it touches a control:

  1) a MULTILINGUAL destructive/consequential lexicon (delete / remove / pay / purchase /
     transfer / deactivate / reset / unsubscribe in the major Latin-script + CJK languages), and
  2) a FAIL-CLOSED posture: the acting engine only touches AFFIRMATIVELY-safe value controls
     (a plain choice/toggle with NO destructive signal). Anything whose safety we cannot
     positively establish is left alone and recorded — never actuate what we can't prove safe.

Pure + string-only; no DOM, no I/O. Matched against a control's accessible label, normalised.
"""
from __future__ import annotations

import re

# Destructive / money-moving / account-consequential intent across the major languages the
# accessible label might use. Whole-word-ish substring match on a normalised label. Kept
# broad on purpose — a false "consequential" only makes the crawler SKIP acting on a control
# (safe), never actuate one it shouldn't.
_CONSEQUENTIAL_TERMS = (
    # delete / remove / erase
    "delete", "remove", "erase", "discard", "destroy", "wipe", "purge", "trash",
    "löschen", "entfernen", "eliminar", "borrar", "quitar", "supprimer", "effacer",
    "elimina", "rimuovi", "excluir", "remover", "apagar", "usuń", "sil", "verwijderen",
    "удалить", "удали", "删除", "移除", "刪除", "削除", "삭제",
    # pay / buy / money movement
    "pay", "payment", "purchase", "buy", "checkout", "place order", "transfer", "send money",
    "withdraw", "wire", "remit", "donate", "subscribe", "renew",
    "zahlen", "bezahlen", "kaufen", "überweisen", "pagar", "comprar", "transferir",
    "payer", "acheter", "virement", "paga", "acquista", "bonifico", "支付", "付款", "购买",
    "转账", "決済", "支払", "送金", "결제", "구매", "송금",
    # account / destructive settings
    "deactivate", "deregister", "close account", "delete account", "cancel subscription",
    "unsubscribe", "reset", "factory reset", "revoke", "terminate", "disable account",
    "deaktivieren", "kündigen", "zurücksetzen", "desactivar", "cancelar", "restablecer",
    "désactiver", "résilier", "réinitialiser", "disattiva", "annulla", "注销", "停用", "重置",
    "退订", "解約", "退会",
)

_NORM_RE = re.compile(r"[\s_\-]+")


def _norm(text: str) -> str:
    return _NORM_RE.sub(" ", str(text or "").strip().lower()).strip()


def is_consequential(label: str) -> bool:
    """True when a control's label carries destructive / money-moving / account-consequential
    intent in ANY of the covered languages — the crawler must not actuate it in exploration."""
    lo = _norm(label)
    if not lo:
        return False
    return any(term in lo for term in _CONSEQUENTIAL_TERMS)


def safe_to_actuate(control) -> bool:
    """FAIL-CLOSED gate for the acting engine: only touch a control that is AFFIRMATIVELY a
    plain value control with no destructive/danger signal. A control already flagged dangerous
    by the base guard, or whose label reads consequential, is never actuated."""
    try:
        if control.get("danger"):
            return False
        if control.get("disabled"):
            return False
        return not is_consequential(control.get("name") or "")
    except Exception:
        return False  # fail-closed on any uncertainty
