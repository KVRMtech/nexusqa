"""
Insurance PII Patterns Extension for Shield Engine.

Extracted from engines/shield-engine/main.py PIIType enum and PIIDetector.PATTERNS.
Defines insurance-specific PII entity types with detection regex patterns.
"""

from __future__ import annotations

from nexus_sdk.plugins.extensions import PIIEntityDefinition, PIIExtension


def build_pii_extension() -> PIIExtension:
    """Build the insurance PII extension for Shield engine."""
    return PIIExtension(
        domain="insurance",
        entity_types=[
            PIIEntityDefinition(
                name="POLICY_NUMBER",
                display_name="Insurance Policy Number",
                description="Policy numbers in common formats (PLY-, POL-, POLICY-)",
                pattern=r"\b(?:PLY|POL|POLICY)[-\s]?\d{4}[-\s]?[A-Z]{2}[-\s]?\d{4,8}\b",
                risk_level="high",
                redaction_format="[POLICY_{index}]",
                examples=["PLY-2024-TX-00012345", "POL 1234 AB 567890"],
            ),
            PIIEntityDefinition(
                name="POLICY_NUMBER_REF",
                display_name="Policy Number Reference",
                description="Policy number mentioned after contextual keywords",
                pattern=r"\bpolicy\s*(?:#|number|no\.?)\s*[:=]?\s*([A-Z0-9\-]{6,20})\b",
                risk_level="high",
                redaction_format="[POLICY_REF_{index}]",
                examples=["policy #ABC-123456", "policy number: XYZ789012"],
            ),
            PIIEntityDefinition(
                name="AGENT_NPN",
                display_name="Agent National Producer Number",
                description="7-10 digit NPN (National Producer Number) for insurance agents",
                pattern=r"\b(?:NPN|agent\s*#?)[-\s:]*(\d{7,10})\b",
                risk_level="high",
                redaction_format="[NPN_{index}]",
                examples=["NPN: 1234567", "agent# 12345678"],
            ),
            PIIEntityDefinition(
                name="CLAIM_NUMBER",
                display_name="Insurance Claim Number",
                description="Claim numbers in common formats (CLM-, CLAIM-)",
                pattern=r"\b(?:CLM|CLAIM)[-\s]?\d{4,12}\b",
                risk_level="high",
                redaction_format="[CLAIM_{index}]",
                examples=["CLM-20240315001", "CLAIM 123456789012"],
            ),
            PIIEntityDefinition(
                name="MIB_CODE",
                display_name="MIB Code",
                description="Medical Information Bureau codes referencing applicant medical history",
                pattern=r"\b(?:MIB|mib)\s*(?:code|#)?\s*[:=]?\s*([A-Z0-9]{3,8})\b",
                risk_level="critical",
                redaction_format="[MIB_{index}]",
                examples=["MIB code: A123", "MIB #XY4567"],
            ),
            PIIEntityDefinition(
                name="COMMISSION_RATE",
                display_name="Agent Commission Rate",
                description="Commission percentage amounts linked to agents",
                pattern=r"\bcommission\s*(?:rate|%)?\s*[:=]?\s*(\d{1,3}(?:\.\d{1,2})?)\s*%",
                risk_level="medium",
                redaction_format="[COMMISSION_{index}]",
                examples=["commission rate: 12.5%", "commission: 8%"],
            ),
            PIIEntityDefinition(
                name="ACCOUNT_NUMBER",
                display_name="Financial Account Number",
                description="Bank or financial account numbers (6-20 digits)",
                pattern=r"\baccount\s*(?:#|number|no\.?)\s*[:=]?\s*(\d{6,20})\b",
                risk_level="critical",
                redaction_format="[ACCOUNT_{index}]",
                examples=["account #123456789", "account number: 00112233445566"],
            ),
            PIIEntityDefinition(
                name="GROUP_NUMBER",
                display_name="Group Insurance Number",
                description="Group policy or certificate numbers",
                pattern=r"\b(?:group|certificate)\s*(?:#|number|no\.?)?\s*[:=]?\s*([A-Z0-9\-]{4,15})\b",
                risk_level="medium",
                redaction_format="[GROUP_{index}]",
                examples=["group #GRP-12345", "certificate number: CERT-001"],
            ),
        ],
        context_rules=[
            {
                "name": "policy_context",
                "description": "Numbers following insurance policy keywords are likely policy numbers",
                "trigger_words": ["policy", "certificate", "endorsement"],
                "pattern_after": r"\s*[:=]?\s*([A-Z0-9\-]{6,20})",
                "entity_type": "POLICY_NUMBER",
            },
            {
                "name": "agent_context",
                "description": "Numbers following agent-related keywords are likely NPNs",
                "trigger_words": ["agent", "producer", "broker", "NPN"],
                "pattern_after": r"\s*[:=]?\s*(\d{7,10})",
                "entity_type": "AGENT_NPN",
            },
            {
                "name": "claim_context",
                "description": "Numbers following claim keywords are likely claim numbers",
                "trigger_words": ["claim", "loss", "incident"],
                "pattern_after": r"\s*[:=]?\s*([A-Z0-9\-]{6,15})",
                "entity_type": "CLAIM_NUMBER",
            },
        ],
    )
