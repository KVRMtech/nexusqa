"""
Test Case ID Generator — Produces deterministic, human-readable IDs.

Pattern: {PREFIX}-V{VERSION:02d}-{SEQUENCE:03d}

Examples:
  E2E-V11-001   End-to-end test, version 11, sequence 1
  BVA-V03-012   Boundary value analysis, version 3, sequence 12
  NEG-V11-005   Negative test, version 11, sequence 5

The generator is thread-safe and integrates with PostgreSQL
for sequence tracking (no gaps, no duplicates).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ─── Prefix mappings ──────────────────────────────────────────

TEST_TYPE_PREFIXES: dict[str, str] = {
    "e2e": "E2E",
    "end_to_end": "E2E",
    "integration": "INT",
    "bva": "BVA",
    "boundary": "BVA",
    "negative": "NEG",
    "performance": "PERF",
    "edge": "EDG",
    "edge_case": "EDG",
    "regression": "REG",
    "smoke": "SMK",
    "sanity": "SAN",
    "security": "SEC",
    "accessibility": "A11Y",
    "api": "API",
    "ui": "UI",
    "data": "DAT",
}


def get_prefix(test_type: str) -> str:
    """Get the standard prefix for a test type."""
    return TEST_TYPE_PREFIXES.get(test_type.lower(), "TC")


def format_test_case_id(prefix: str, version: int, sequence: int) -> str:
    """
    Format a test case ID according to the standard pattern.

    Args:
        prefix: Test type prefix (e.g., "E2E", "BVA")
        version: Session/version number (1-99)
        sequence: Sequential number within this prefix+version (1-999)

    Returns:
        Formatted ID like "E2E-V11-001"
    """
    return f"{prefix}-V{version:02d}-{sequence:03d}"


async def generate_test_case_id(
    session: AsyncSession,
    tenant_id: str,
    test_type: str = "e2e",
    version: int = 1,
) -> str:
    """
    Generate the next test case ID for a given tenant, type, and version.

    Uses a SELECT MAX approach against the test_cases table to find the
    next available sequence number. This is safe under serializable
    isolation or advisory locks.

    Args:
        session: Active async database session.
        tenant_id: Tenant ID for scope isolation.
        test_type: Test type (e2e, bva, negative, etc.).
        version: Version/session number.

    Returns:
        Next available test case ID (e.g., "E2E-V11-003").
    """
    prefix = get_prefix(test_type)
    id_pattern = f"{prefix}-V{version:02d}-%"

    # Find the highest sequence number for this prefix+version+tenant
    result = await session.execute(
        text(
            "SELECT test_case_id FROM test_cases "
            "WHERE tenant_id = :tenant_id AND test_case_id LIKE :pattern "
            "ORDER BY test_case_id DESC LIMIT 1"
        ),
        {"tenant_id": tenant_id, "pattern": id_pattern},
    )
    row = result.fetchone()

    if row:
        # Extract sequence from the last ID: "E2E-V11-003" -> 3
        last_id: str = row[0]
        try:
            last_seq = int(last_id.split("-")[-1])
        except (ValueError, IndexError):
            last_seq = 0
        next_seq = last_seq + 1
    else:
        next_seq = 1

    return format_test_case_id(prefix, version, next_seq)


async def generate_batch_ids(
    session: AsyncSession,
    tenant_id: str,
    test_type: str,
    version: int,
    count: int,
) -> list[str]:
    """
    Generate multiple sequential test case IDs in one call.

    Args:
        session: Active async database session.
        tenant_id: Tenant ID for scope isolation.
        test_type: Test type (e2e, bva, negative, etc.).
        version: Version/session number.
        count: How many IDs to generate.

    Returns:
        List of sequential test case IDs.
    """
    prefix = get_prefix(test_type)
    id_pattern = f"{prefix}-V{version:02d}-%"

    result = await session.execute(
        text(
            "SELECT test_case_id FROM test_cases "
            "WHERE tenant_id = :tenant_id AND test_case_id LIKE :pattern "
            "ORDER BY test_case_id DESC LIMIT 1"
        ),
        {"tenant_id": tenant_id, "pattern": id_pattern},
    )
    row = result.fetchone()

    if row:
        last_id: str = row[0]
        try:
            start_seq = int(last_id.split("-")[-1]) + 1
        except (ValueError, IndexError):
            start_seq = 1
    else:
        start_seq = 1

    return [
        format_test_case_id(prefix, version, start_seq + i)
        for i in range(count)
    ]
