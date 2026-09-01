"""
Shield Engine — Unit Tests.

Tests PII detection, redaction, reveal, and audit logging.
"""

import pytest
import sys
import os

# Add project paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "shield-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


class TestPIIDetector:
    """Test PII detection patterns."""

    def setup_method(self):
        from main import PIIDetector
        self.detector = PIIDetector()

    # ── SSN Detection ──────────────────────────────────────

    def test_ssn_dashed(self):
        entities = self.detector.detect("SSN is 123-45-6789")
        ssn_entities = [e for e in entities if e["type"] == "SSN"]
        assert len(ssn_entities) >= 1
        assert "123-45-6789" in ssn_entities[0]["value"]

    def test_ssn_spaced(self):
        entities = self.detector.detect("SSN: 123 45 6789")
        ssn_entities = [e for e in entities if e["type"] == "SSN"]
        assert len(ssn_entities) >= 1

    # ── Email Detection ────────────────────────────────────

    def test_email(self):
        entities = self.detector.detect("Contact john.smith@example.com for info")
        email_entities = [e for e in entities if e["type"] == "EMAIL"]
        assert len(email_entities) == 1
        assert email_entities[0]["value"] == "john.smith@example.com"

    # ── Phone Detection ────────────────────────────────────

    def test_phone_dashed(self):
        entities = self.detector.detect("Call 555-123-4567 today")
        phone_entities = [e for e in entities if e["type"] == "PHONE"]
        assert len(phone_entities) >= 1

    def test_phone_parentheses(self):
        entities = self.detector.detect("Call (555) 123-4567")
        phone_entities = [e for e in entities if e["type"] == "PHONE"]
        assert len(phone_entities) >= 1

    # ── Named Person Detection ─────────────────────────────

    def test_name_with_prefix(self):
        entities = self.detector.detect("Mr. John Smith is enrolled")
        name_entities = [e for e in entities if e["type"] == "PERSON_NAME"]
        assert len(name_entities) >= 1

    def test_name_with_dr(self):
        entities = self.detector.detect("Dr. Sarah Williams examined the patient")
        name_entities = [e for e in entities if e["type"] == "PERSON_NAME"]
        assert len(name_entities) >= 1

    # ── Date of Birth ──────────────────────────────────────

    def test_dob(self):
        entities = self.detector.detect("DOB: 03/15/1992")
        dob_entities = [e for e in entities if e["type"] == "DATE_OF_BIRTH"]
        assert len(dob_entities) >= 1

    # ── Insurance-Specific ─────────────────────────────────

    def test_policy_number(self):
        entities = self.detector.detect("Policy PLY-2024-AB-12345678")
        pol_entities = [e for e in entities if e["type"] == "POLICY_NUMBER"]
        assert len(pol_entities) >= 1

    def test_claim_number(self):
        entities = self.detector.detect("Claim CLM-20240155 is under review")
        claim_entities = [e for e in entities if e["type"] == "CLAIM_NUMBER"]
        assert len(claim_entities) >= 1

    def test_agent_npn(self):
        entities = self.detector.detect("Agent NPN: 1234567890")
        npn_entities = [e for e in entities if e["type"] == "AGENT_NPN"]
        assert len(npn_entities) >= 1

    # ── Credit Card ────────────────────────────────────────

    def test_credit_card_dashed(self):
        entities = self.detector.detect("Card: 4111-1111-1111-1111")
        cc_entities = [e for e in entities if e["type"] == "CREDIT_CARD"]
        assert len(cc_entities) >= 1

    # ── Multiple PII in One Text ───────────────────────────

    def test_multiple_pii(self):
        text = (
            "Mr. John Smith, SSN 123-45-6789, "
            "email: john@example.com, phone: (555) 867-5309, "
            "policy PLY-2024-AB-99887766"
        )
        entities = self.detector.detect(text)
        types_found = set(e["type"] for e in entities)
        # Should detect at least SSN, email, phone
        assert "SSN" in types_found
        assert "EMAIL" in types_found
        assert "PHONE" in types_found

    # ── No PII ─────────────────────────────────────────────

    def test_clean_text(self):
        entities = self.detector.detect(
            "The premium rate for age band 35-40 is 4.25 per thousand"
        )
        # Should find no or very few entities
        assert len(entities) <= 1

    # ── Custom Patterns ────────────────────────────────────

    def test_custom_pattern(self):
        custom = {"INTERNAL_ID": r"INS-\d{8}"}
        entities = self.detector.detect(
            "Reference: INS-12345678", custom_patterns=custom
        )
        custom_entities = [e for e in entities if e["type"] == "INTERNAL_ID"]
        assert len(custom_entities) == 1


class TestPIIRedactor:
    """Test PII redaction and reveal."""

    def setup_method(self):
        from main import PIIDetector, PIIRedactor, RedactionStore, ShieldConfig
        self.detector = PIIDetector()
        config = ShieldConfig()
        self.store = RedactionStore(config)
        self.redactor = PIIRedactor(self.store)

    async def test_redact_ssn(self):
        text = "SSN is 123-45-6789"
        entities = self.detector.detect(text)
        safe_text, mapping_id, mapping = await self.redactor.redact(text, entities)

        assert "123-45-6789" not in safe_text
        assert "[SSN_" in safe_text
        assert mapping_id is not None
        assert len(mapping) >= 1

    async def test_redact_preserves_structure(self):
        text = "Mr. John Smith has SSN 123-45-6789 and email john@test.com"
        entities = self.detector.detect(text)
        safe_text, mapping_id, mapping = await self.redactor.redact(text, entities)

        # Original PII should be gone
        assert "123-45-6789" not in safe_text
        assert "john@test.com" not in safe_text

        # Tokens should be present
        assert "[SSN_" in safe_text or "[EMAIL_" in safe_text

    async def test_reveal_returns_mapping(self):
        text = "SSN is 123-45-6789"
        entities = self.detector.detect(text)
        _, mapping_id, _ = await self.redactor.redact(text, entities)

        revealed = await self.redactor.reveal(mapping_id)
        assert revealed is not None
        assert any("123-45-6789" in v for v in revealed.values())

    async def test_reveal_unknown_id_returns_none(self):
        result = await self.redactor.reveal("nonexistent-id")
        assert result is None

    async def test_restore_original_text(self):
        text = "Call 555-123-4567 for details"
        entities = self.detector.detect(text)
        safe_text, mapping_id, _ = await self.redactor.redact(text, entities)

        restored = await self.redactor.restore(safe_text, mapping_id)
        assert restored is not None
        assert "555-123-4567" in restored


class TestShieldAuditLog:
    """Test audit logging."""

    async def test_record_and_retrieve(self):
        from main import ShieldAuditLog
        # Clear existing and ensure we use in-memory mode
        ShieldAuditLog._log = []
        ShieldAuditLog._connected = False
        ShieldAuditLog._redis = None

        await ShieldAuditLog.record(
            action="redact",
            tenant_id="t-001",
            user_id="u-001",
            mapping_id="m-001",
            entity_count=3,
            entity_types=["SSN", "EMAIL"],
        )

        logs = await ShieldAuditLog.get_log(tenant_id="t-001")
        assert len(logs) == 1
        assert logs[0]["action"] == "redact"
        assert logs[0]["entity_count"] == 3

    async def test_filter_by_tenant(self):
        from main import ShieldAuditLog
        ShieldAuditLog._log = []
        ShieldAuditLog._connected = False
        ShieldAuditLog._redis = None

        await ShieldAuditLog.record("redact", "t-001", "u-001", "m-001", 1, ["SSN"])
        await ShieldAuditLog.record("redact", "t-002", "u-002", "m-002", 2, ["EMAIL"])

        logs_t1 = await ShieldAuditLog.get_log(tenant_id="t-001")
        logs_t2 = await ShieldAuditLog.get_log(tenant_id="t-002")
        assert len(logs_t1) == 1
        assert len(logs_t2) == 1


class TestRiskLevel:
    """Test risk level calculation in analyze."""

    def setup_method(self):
        from main import PIIDetector
        self.detector = PIIDetector()

    def test_critical_with_ssn(self):
        entities = self.detector.detect("SSN: 123-45-6789")
        found_types = set(e["type"] for e in entities)
        critical_types = {"SSN", "CREDIT_CARD"}
        assert found_types & critical_types  # Should be critical

    def test_clean_text_low_risk(self):
        entities = self.detector.detect("Premium rate is 4.25")
        assert len(entities) == 0  # No entities = low risk
