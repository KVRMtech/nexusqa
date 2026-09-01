"""SCIM model validation — imports inside tests to stay isolated."""

from __future__ import annotations

import pytest


def test_user_minimum_fields_validate() -> None:
    from app.scim import SCIMUserResource

    u = SCIMUserResource(userName="alice@acme.com")
    assert u.userName == "alice@acme.com"
    assert u.active is True


def test_user_username_required() -> None:
    from app.scim import SCIMUserResource

    with pytest.raises(Exception):
        SCIMUserResource(userName="")


def test_user_primary_email_resolution() -> None:
    from app.scim import SCIMEmail, SCIMUserResource

    u = SCIMUserResource(
        userName="alice@acme.com",
        emails=[
            SCIMEmail(value="alice.work@acme.com", primary=False),
            SCIMEmail(value="alice@acme.com", primary=True),
            SCIMEmail(value="alice.alt@acme.com"),
        ],
    )
    assert u.primary_email() == "alice@acme.com"


def test_user_primary_email_falls_back_to_first() -> None:
    from app.scim import SCIMEmail, SCIMUserResource

    u = SCIMUserResource(
        userName="alice@acme.com",
        emails=[SCIMEmail(value="alice@acme.com")],
    )
    assert u.primary_email() == "alice@acme.com"


def test_user_primary_email_none_when_no_emails() -> None:
    from app.scim import SCIMUserResource

    u = SCIMUserResource(userName="alice@acme.com")
    assert u.primary_email() is None


def test_enterprise_user_passes_through() -> None:
    from app.scim import SCIMEnterpriseUser, SCIMUserResource

    u = SCIMUserResource(
        userName="alice",
        enterpriseUser=SCIMEnterpriseUser(
            department="Sales",
            manager={"value": "manager-1"},
        ),
    )
    assert u.enterpriseUser is not None
    assert u.enterpriseUser.department == "Sales"


def test_group_minimum_fields() -> None:
    from app.scim import SCIMGroupResource

    g = SCIMGroupResource(displayName="Engineering")
    assert g.displayName == "Engineering"
    assert g.members == []


def test_group_with_members() -> None:
    from app.scim import SCIMGroupMember, SCIMGroupResource

    g = SCIMGroupResource(
        displayName="Engineering",
        members=[
            SCIMGroupMember(value="user-1", type="User"),
            SCIMGroupMember(value="user-2", type="User"),
        ],
    )
    assert {m.value for m in g.members} == {"user-1", "user-2"}


def test_patch_op_normalises_case() -> None:
    from app.scim import SCIMPatchOperation

    op = SCIMPatchOperation(op="ADD", path="active", value=True)
    assert op.op == "add"


def test_patch_op_rejects_unknown_op() -> None:
    from app.scim import SCIMPatchOperation

    with pytest.raises(Exception):
        SCIMPatchOperation(op="merge", value="x")


def test_patch_request_requires_at_least_one_op() -> None:
    from app.scim import SCIMPatchRequest

    with pytest.raises(Exception):
        SCIMPatchRequest(Operations=[])


def test_patch_request_round_trip() -> None:
    from app.scim import SCIMPatchOperation, SCIMPatchRequest

    req = SCIMPatchRequest(
        Operations=[
            SCIMPatchOperation(op="replace", path="active", value=False),
            SCIMPatchOperation(op="add", path="title", value="Director"),
        ]
    )
    assert len(req.Operations) == 2
    assert req.Operations[0].op == "replace"
