"""M3.3 fleet-proof suite — shared path bootstrap and FLEET ISOLATION.

WHY THE PURGE FIXTURE EXISTS
============================
These proofs run against a persistent Postgres, and the queue is FLEET-WIDE by
design: ``plan_drain`` reads every tenant's queued crawls and interleaves them
round-robin. That is the correct production behaviour and exactly what the
fairness proof asserts — but it also means leftover rows from an EARLIER test
(or an earlier run of the whole suite) are indistinguishable from live fleet
work.

The concrete failure that motivated this: with dozens of accumulated test
tenants each holding queued rows, a round-robin batch of ten gives one slot per
tenant, so the tenant under test never appeared in the plan at all and a correct
implementation looked broken. It also made the suite progressively slower, since
the fleet scan opens a connection per tenant.

So every test starts from a clean fleet: all explorations belonging to tenants
this suite created are deleted, and those tenants are deregistered. Deletion is
done PER TENANT under that tenant's ``nexus.current_tenant_id`` GUC, because
``qe_explorations`` is RLS-protected — the fixture is subject to exactly the same
isolation as the code under test, and could not delete another tenant's rows
even by accident.

Only tenants matching this suite's own prefix are touched, so a developer
pointing these tests at a database that also holds other fixtures loses nothing
of theirs.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONTRACT = os.path.join(os.path.dirname(_HERE), "contract")
if _CONTRACT not in sys.path:
    sys.path.insert(0, _CONTRACT)

#: Every tenant this suite mints is named ``tfl<NN>_…``. The purge is scoped to
#: that prefix so it can never touch data the suite did not create.
TEST_TENANT_PREFIX = "tfl"

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
SUBSTRATE_DB_URL = os.environ.get("QEC_TEST_SUBSTRATE_DATABASE_URL", "")
#: A SUPERUSER dsn ON THE SUBSTRATE DATABASE, used ONLY to deregister this
#: suite's own test tenants.
#:
#: WHY NOT THE SUBSTRATE ROLE. The purge below used it, and it cannot work: the
#: production bootstrap grants `qec_substrate` SELECT and INSERT on `tenants` and
#: deliberately withholds the rest —
#:
#:     scripts/qec_db_bootstrap.sql:103
#:       GRANT SELECT, INSERT ON tenants TO qec_substrate;
#:       -- No UPDATE/DELETE - existing tenants are never touched.
#:
#: so the DELETE raises InsufficientPrivilegeError: permission denied for table
#: tenants. The fix is test-side on purpose: granting DELETE would widen a
#: least-privilege role someone narrowed deliberately, on the table tenant
#: isolation is anchored on, to make a cleanup fixture convenient.
#:
#: WHY *THIS* VARIABLE AND NOT QEC_TEST_ADMIN_DATABASE_URL. The first fix reached
#: for the admin dsn and swapped one failure for another — 294 privilege errors
#: became 490 `relation "tenants" does not exist`. The admin dsn is superuser on
#: the **maintenance** database (`.../postgres`), because its documented job is
#: CREATE/DROP of throwaway databases; `tenants` lives in `nexus`. Ample
#: privilege, wrong database.
#:
#:     QEC_TEST_SUBSTRATE_DATABASE_URL  .../nexus     qec_substrate  no DELETE
#:     QEC_TEST_DATABASE_URL            .../nexus     postgres       <- this one
#:     QEC_TEST_ADMIN_DATABASE_URL      .../postgres  postgres       no `tenants`
#:
#: The name says which DATABASE it must point at, not merely how privileged it
#: is, because privilege was never the part that was hard to get right.
#:
#: Falls back to the substrate engine when unset, which is the pre-existing
#: behaviour and still correct on a laptop whose substrate dsn is a superuser.
SUPERUSER_SUBSTRATE_DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")


@pytest.fixture(autouse=True)
def _clean_fleet():
    """Delete this suite's tenants and their crawls before every test."""
    if not (QEC_DB_URL and SUBSTRATE_DB_URL):
        yield
        return

    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def _purge() -> None:
        sub = create_async_engine(SUBSTRATE_DB_URL, poolclass=NullPool)
        qec = create_async_engine(QEC_DB_URL, poolclass=NullPool)
        try:
            async with sub.begin() as conn:
                rows = (await conn.execute(text(
                    "SELECT tenant_id FROM tenants WHERE tenant_id LIKE :p"),
                    {"p": TEST_TENANT_PREFIX + "%"})).all()
            tenants = [str(r[0]) for r in rows]
            if tenants:
                # RLS forces a per-tenant scope even to delete our own rows.
                async with qec.begin() as conn:
                    for t in tenants:
                        await conn.execute(text(
                            "SELECT set_config('nexus.current_tenant_id', :t, true)"),
                            {"t": t})
                        await conn.execute(
                            text("DELETE FROM qe_explorations WHERE tenant_id = :t"),
                            {"t": t})
                        await conn.execute(
                            text("DELETE FROM client_apps WHERE tenant_id = :t"),
                            {"t": t})
                # Deregistration goes through a superuser dsn ON THE SUBSTRATE
                # DATABASE — see SUPERUSER_SUBSTRATE_DB_URL.
                # The two deletes above stay on `qec` under the tenant GUC on
                # purpose: qe_explorations is RLS-protected and the fixture
                # should be subject to the same isolation as the code it tests.
                # Only this one needs a privilege the substrate role is
                # deliberately denied.
                purge = create_async_engine(
                    SUPERUSER_SUBSTRATE_DB_URL or SUBSTRATE_DB_URL, poolclass=NullPool)
                try:
                    async with purge.begin() as conn:
                        await conn.execute(text(
                            "DELETE FROM tenants WHERE tenant_id LIKE :p"),
                            {"p": TEST_TENANT_PREFIX + "%"})
                finally:
                    await purge.dispose()
        finally:
            await qec.dispose()
            await sub.dispose()

    asyncio.run(_purge())
    yield
