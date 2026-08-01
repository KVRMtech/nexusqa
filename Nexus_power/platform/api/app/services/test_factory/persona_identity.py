"""Is this actually a member of this application, and is it still in service?

``persona_id`` arrives as a declared input — from the portal, from CI, from a saved
pipeline config — and dispatch never checked it against anything. Three ways that
goes wrong, all of them silent:

  * **A RETIRED member keeps running.** ``retire`` sets ``status='retired'`` and
    ``list_personas`` stops offering it, so it vanishes from the picker — but a CI
    job with the id pinned in its config carries on authenticating as a
    decommissioned account. The screen says the member is gone; the pipeline says
    otherwise, and the pipeline is the one producing evidence.
  * **A member of a DIFFERENT application is accepted.** ``get_persona`` filters by
    tenant only, so any persona_id in the tenant resolves. Its card is then used
    against this artifact's login, and the run reports a member that has nothing to
    do with the app under test.
  * **An id that resolves to nothing runs anyway.** With no persona row the run
    proceeds down the form-login path and reports the persona_id it was given — a
    member that does not exist.

None of these is an application failure, and none should reach the runner. The
refusal has to be at DISPATCH, not in the picker: the picker is a convenience, and
every one of these arrives from callers that never see it.

Pure — no DB, no I/O. The caller loads the persona row and applies the verdict.
"""
from __future__ import annotations

__all__ = ["check_persona", "PERSONA0_PREFIX"]

#: The synthetic persona that represents an app's legacy form-login. It has no row;
#: its identity IS the artifact it names.
PERSONA0_PREFIX = "persona0::"


def check_persona(*, persona_id: str, persona: dict | None, artifact_id: str) -> dict:
    """Decide whether this member may be run as, for this artifact.

    ``persona_id``  what the caller declared ('' = no member; the form-login path)
    ``persona``     the loaded row (persona_store.get_persona), or None
    ``artifact_id`` the application this run is for

    Returns ``{allowed, reason, detail}``. ``detail.note`` is the sentence an
    operator reads; every refusal names the member and says plainly that the
    application under test is not implicated.
    """
    pid = str(persona_id or "").strip()

    # No member declared — the form-login path, unchanged. Nothing is claimed about
    # an identity, so there is nothing to verify.
    if not pid:
        return _ok()

    # persona-0 is synthetic: no row exists, and its id encodes the artifact it
    # belongs to. Checking that suffix is the only thing that stops one app's
    # legacy login being run against another's.
    if pid.startswith(PERSONA0_PREFIX):
        owner = pid[len(PERSONA0_PREFIX):]
        if owner and artifact_id and owner != artifact_id:
            return _no("persona_belongs_to_another_application",
                       {"persona_id": pid, "artifact_id": artifact_id,
                        "persona_artifact_id": owner},
                       note=("This run names the default login of a DIFFERENT "
                             "application. Its credentials have nothing to do with "
                             "the app under test, so the run is refused rather than "
                             "executed under a borrowed identity. This is a "
                             "configuration error, NOT an application failure."))
        return _ok()

    if not persona:
        return _no("persona_not_registered",
                   {"persona_id": pid, "artifact_id": artifact_id},
                   note=("This run names a member that does not exist. Running anyway "
                         "would execute unauthenticated while the report named a "
                         "member nobody defined. Check the id, or define the member "
                         "in Members & Environments. This is a configuration error, "
                         "NOT an application failure."))

    owner = str(persona.get("artifact_id") or "")
    if artifact_id and owner and owner != artifact_id:
        return _no("persona_belongs_to_another_application",
                   {"persona_id": pid, "artifact_id": artifact_id,
                    "persona_artifact_id": owner,
                    "persona_name": str(persona.get("name") or "")},
                   note=("This member belongs to a different application. Its "
                         "credentials are for that application's login, so running it "
                         "here would either fail or authenticate as somebody "
                         "unrelated to what is being tested. Refused. This is a "
                         "configuration error, NOT an application failure."))

    status = str(persona.get("status") or "active").lower()
    if status != "active":
        return _no("persona_retired",
                   {"persona_id": pid, "status": status,
                    "persona_name": str(persona.get("name") or "")},
                   note=("This member was retired, so it is no longer offered for "
                         "selection — but this run still names it, which usually "
                         "means a saved pipeline or CI job pins the id. Running would "
                         "authenticate as a decommissioned account and report results "
                         "under it. Point the run at an active member. This is a "
                         "configuration decision, NOT an application failure."))

    return _ok()


def _ok() -> dict:
    return {"allowed": True, "reason": "", "detail": {}}


def _no(reason: str, detail: dict, *, note: str) -> dict:
    return {"allowed": False, "reason": reason, "detail": dict(detail, note=note)}
