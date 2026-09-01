"""
Shield Engine — Modular Sub-package Tests.

Tests the detectors and redactors modules refactored from
the monolithic shield-engine/main.py.

All tests exercise the classes directly (no Redis required).
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "shield-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── PII Detector ─────────────────────────────────────────────


class TestPIIType:
    """Test PIIType enum from app.detectors."""

    def test_import(self):
        from app.detectors import PIIType
        assert PIIType is not None

    def test_standard_types(self):
        from app.detectors import PIIType
        assert PIIType.SSN is not None
        assert PIIType.EMAIL is not None
        assert PIIType.PHONE is not None
        assert PIIType.CREDIT_CARD is not None

    def test_insurance_types(self):
        from app.detectors import PIIType
        assert PIIType.POLICY_NUMBER is not None
        assert PIIType.AGENT_NPN is not None
        assert PIIType.CLAIM_NUMBER is not None

    def test_enum_values_are_strings(self):
        from app.detectors import PIIType
        for member in PIIType:
            assert isinstance(member.value, str)


class TestPIIDetector:
    """Test PIIDetector from app.detectors."""

    def test_import(self):
        from app.detectors import PIIDetector
        assert PIIDetector is not None

    def test_detect_ssn(self):
        from app.detectors import PIIDetector, PIIType
        d = PIIDetector()
        hits = d.detect("My SSN is 123-45-6789 and that's private.")
        types = {h["type"] for h in hits}
        assert PIIType.SSN.value in types or PIIType.SSN in types

    def test_detect_email(self):
        from app.detectors import PIIDetector, PIIType
        d = PIIDetector()
        hits = d.detect("Contact me at john@example.com please.")
        types = {h["type"] for h in hits}
        assert PIIType.EMAIL.value in types or PIIType.EMAIL in types

    def test_detect_phone(self):
        from app.detectors import PIIDetector, PIIType
        d = PIIDetector()
        hits = d.detect("Call me at (555) 123-4567.")
        types = {h["type"] for h in hits}
        assert PIIType.PHONE.value in types or PIIType.PHONE in types

    def test_detect_credit_card(self):
        from app.detectors import PIIDetector, PIIType
        d = PIIDetector()
        hits = d.detect("Card number is 4111-1111-1111-1111.")
        types = {h["type"] for h in hits}
        assert PIIType.CREDIT_CARD.value in types or PIIType.CREDIT_CARD in types

    def test_detect_no_pii(self):
        from app.detectors import PIIDetector
        d = PIIDetector()
        hits = d.detect("This is a perfectly clean sentence.")
        assert len(hits) == 0

    def test_detect_multiple(self):
        from app.detectors import PIIDetector
        d = PIIDetector()
        text = "SSN: 123-45-6789, email: test@mail.com, phone: 555-123-4567"
        hits = d.detect(text)
        assert len(hits) >= 3

    def test_custom_patterns(self):
        from app.detectors import PIIDetector
        d = PIIDetector()
        custom = {"CUSTOM_ID": r"ID-\d{6}"}
        hits = d.detect("Your ID is ID-123456.", custom_patterns=custom)
        types = {h["type"] for h in hits}
        assert "CUSTOM_ID" in types

    def test_detect_returns_match_text(self):
        from app.detectors import PIIDetector
        d = PIIDetector()
        hits = d.detect("My SSN is 123-45-6789.")
        assert len(hits) >= 1
        hit = hits[0]
        assert "value" in hit


# ─── Redaction Store ──────────────────────────────────────────


class TestRedactionStore:
    """Test RedactionStore (in-memory fallback) from app.redactors."""

    def test_import(self):
        from app.redactors import RedactionStore
        assert RedactionStore is not None

    @pytest.mark.asyncio
    async def test_in_memory_save_load(self):
        from app.redactors import RedactionStore
        from main import ShieldConfig
        cfg = ShieldConfig()
        store = RedactionStore(cfg)
        # Should work without Redis (in-memory fallback)
        await store.save("map_1", {"[SSN_1]": "123-45-6789"})
        loaded = await store.load("map_1")
        assert loaded is not None
        assert loaded["[SSN_1]"] == "123-45-6789"

    @pytest.mark.asyncio
    async def test_load_missing_key(self):
        from app.redactors import RedactionStore
        from main import ShieldConfig
        cfg = ShieldConfig()
        store = RedactionStore(cfg)
        result = await store.load("nonexistent_key")
        assert result is None


# ─── PII Redactor ─────────────────────────────────────────────


class TestPIIRedactor:
    """Test PIIRedactor (token-based replacement) from app.redactors."""

    def test_import(self):
        from app.redactors import PIIRedactor
        assert PIIRedactor is not None

    @pytest.mark.asyncio
    async def test_redact_basic(self):
        from app.redactors import PIIRedactor, RedactionStore
        from main import ShieldConfig
        cfg = ShieldConfig()
        store = RedactionStore(cfg)
        r = PIIRedactor(store)
        text = "My SSN is 123-45-6789"
        entities = [{"type": "SSN", "value": "123-45-6789", "start": 10, "end": 21}]
        safe_text, mapping_id, mapping = await r.redact(text, entities)
        assert "123-45-6789" not in safe_text
        assert "[SSN_" in safe_text
        assert len(mapping) >= 1

    @pytest.mark.asyncio
    async def test_redact_multiple(self):
        from app.redactors import PIIRedactor, RedactionStore
        from main import ShieldConfig
        cfg = ShieldConfig()
        store = RedactionStore(cfg)
        r = PIIRedactor(store)
        text = "SSN 123-45-6789 and email test@mail.com"
        entities = [
            {"type": "SSN", "value": "123-45-6789", "start": 4, "end": 15},
            {"type": "EMAIL", "value": "test@mail.com", "start": 26, "end": 39},
        ]
        safe_text, mapping_id, mapping = await r.redact(text, entities)
        assert "123-45-6789" not in safe_text
        assert "test@mail.com" not in safe_text
        assert len(mapping) >= 2

    @pytest.mark.asyncio
    async def test_restore(self):
        from app.redactors import PIIRedactor, RedactionStore
        from main import ShieldConfig
        cfg = ShieldConfig()
        store = RedactionStore(cfg)
        r = PIIRedactor(store)
        text = "My SSN is 123-45-6789"
        entities = [{"type": "SSN", "value": "123-45-6789", "start": 10, "end": 21}]
        safe_text, mapping_id, mapping = await r.redact(text, entities)
        restored = await r.restore(safe_text, mapping_id)
        assert "123-45-6789" in restored

    @pytest.mark.asyncio
    async def test_reveal(self):
        from app.redactors import PIIRedactor, RedactionStore
        from main import ShieldConfig
        cfg = ShieldConfig()
        store = RedactionStore(cfg)
        r = PIIRedactor(store)
        text = "Email is test@mail.com"
        entities = [{"type": "EMAIL", "value": "test@mail.com", "start": 9, "end": 22}]
        safe_text, mapping_id, mapping = await r.redact(text, entities)
        revealed = await r.reveal(mapping_id)
        assert revealed is not None
        assert "test@mail.com" in revealed.values()


# ─── Shield Audit Log ─────────────────────────────────────────


class TestShieldAuditLog:
    """Test ShieldAuditLog (in-memory) from app.redactors."""

    def test_import(self):
        from app.redactors import ShieldAuditLog
        assert ShieldAuditLog is not None

    @pytest.mark.asyncio
    async def test_record_and_get_log(self):
        from app.redactors import ShieldAuditLog
        # Reset in-memory log
        ShieldAuditLog._log = []
        ShieldAuditLog._connected = False
        await ShieldAuditLog.record(
            action="redact",
            tenant_id="t1",
            user_id="u1",
            mapping_id="m1",
            entity_count=3,
            entity_types=["SSN", "EMAIL", "PHONE"],
        )
        log = await ShieldAuditLog.get_log(limit=10)
        assert len(log) >= 1
        assert log[0]["action"] == "redact"

    @pytest.mark.asyncio
    async def test_get_log_limit(self):
        from app.redactors import ShieldAuditLog
        ShieldAuditLog._log = []
        ShieldAuditLog._connected = False
        for i in range(20):
            await ShieldAuditLog.record(
                action=f"action_{i}",
                tenant_id="t1",
                user_id="u1",
                mapping_id=f"m{i}",
                entity_count=1,
                entity_types=["SSN"],
            )
        log = await ShieldAuditLog.get_log(limit=5)
        assert len(log) == 5


# ─── Integration: Main module imports from sub-packages ───────


class TestShieldMainImports:
    """Verify main.py v0.2.0 correctly imports from sub-packages."""

    def test_main_version(self):
        from main import ShieldEngine
        engine = ShieldEngine()
        assert engine.version == "0.2.0"

    def test_main_imports_detectors(self):
        from main import PIIDetector, PIIType
        assert PIIDetector is not None
        assert PIIType is not None

    def test_main_imports_redactors(self):
        from main import PIIRedactor, RedactionStore, ShieldAuditLog
        assert PIIRedactor is not None
        assert RedactionStore is not None
        assert ShieldAuditLog is not None
