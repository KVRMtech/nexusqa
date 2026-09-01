"""
Shield Engine — Pattern-based PII Detection.

Detects PII entities using compiled regex patterns for both standard
PII (SSN, name, DOB, phone, email, credit card) and insurance-specific
PII (policy numbers, agent NPNs, claim numbers, MIB codes).
"""

from __future__ import annotations

import re
import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ─── PII Entity Types ─────────────────────────────────────────

class PIIType(str, Enum):
    SSN = "SSN"
    PERSON_NAME = "PERSON_NAME"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    ADDRESS = "ADDRESS"
    CREDIT_CARD = "CREDIT_CARD"
    # Insurance-specific
    POLICY_NUMBER = "POLICY_NUMBER"
    AGENT_NPN = "AGENT_NPN"
    CLAIM_NUMBER = "CLAIM_NUMBER"
    MIB_CODE = "MIB_CODE"
    ACCOUNT_NUMBER = "ACCOUNT_NUMBER"
    COMMISSION_RATE = "COMMISSION_RATE"


# ─── PII Detector (Pattern-Based) ─────────────────────────────

class PIIDetector:
    """
    Detects PII entities using regex patterns.

    In Phase 1, this uses pattern matching.
    Phase 2 adds Microsoft Presidio + Phi-3 for context-aware detection.
    """

    # Standard PII patterns
    PATTERNS: dict[PIIType, list[re.Pattern]] = {
        PIIType.SSN: [
            re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
            re.compile(r'\b\d{3}\s\d{2}\s\d{4}\b'),
            re.compile(r'\b\d{9}\b(?=.*(?:ssn|social|security))', re.IGNORECASE),
        ],
        PIIType.PERSON_NAME: [
            # Common name patterns — enhanced later with NER model
            re.compile(
                r'\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b'
            ),
        ],
        PIIType.DATE_OF_BIRTH: [
            re.compile(
                r'\b(?:DOB|date of birth|born|birthday)[:\s]*'
                r'(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\b',
                re.IGNORECASE,
            ),
        ],
        PIIType.PHONE: [
            re.compile(r'\b\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'),
            re.compile(r'\b\+1[-.\s]?\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b'),
        ],
        PIIType.EMAIL: [
            re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
        ],
        PIIType.CREDIT_CARD: [
            re.compile(r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b'),
        ],
        # Insurance-specific
        PIIType.POLICY_NUMBER: [
            re.compile(r'\b(?:PLY|POL|POLICY)[-\s]?\d{4}[-\s]?[A-Z]{2}[-\s]?\d{4,8}\b', re.IGNORECASE),
            re.compile(r'\bpolicy\s*(?:#|number|no\.?)\s*[:=]?\s*([A-Z0-9\-]+)\b', re.IGNORECASE),
        ],
        PIIType.AGENT_NPN: [
            re.compile(r'\b(?:NPN|agent\s*#?)[-\s:]*(\d{7,10})\b', re.IGNORECASE),
        ],
        PIIType.CLAIM_NUMBER: [
            re.compile(r'\b(?:CLM|CLAIM)[-\s]?\d{4,12}\b', re.IGNORECASE),
        ],
        PIIType.ACCOUNT_NUMBER: [
            re.compile(r'\baccount\s*(?:#|number|no\.?)\s*[:=]?\s*(\d{6,20})\b', re.IGNORECASE),
        ],
    }

    def detect(
        self,
        text: str,
        custom_patterns: Optional[dict[str, str]] = None,
    ) -> list[dict]:
        """
        Detect all PII entities in text.

        Parameters
        ----------
        text : str
            The input text to scan.
        custom_patterns : dict[str, str] | None
            Additional ``{name: regex_pattern}`` for tenant-specific PII.

        Returns
        -------
        list[dict]
            Each dict has keys:
            ``type``, ``value``, ``start``, ``end``, ``confidence``, ``detector``.
        """
        entities: list[dict] = []

        # Standard patterns
        for pii_type, patterns in self.PATTERNS.items():
            for pattern in patterns:
                for match in pattern.finditer(text):
                    entities.append({
                        "type": pii_type.value if hasattr(pii_type, 'value') else str(pii_type),
                        "value": match.group(),
                        "start": match.start(),
                        "end": match.end(),
                        "confidence": 0.95,
                        "detector": "pattern",
                    })

        # Custom patterns (per-tenant)
        if custom_patterns:
            for name, pattern_str in custom_patterns.items():
                try:
                    pattern = re.compile(pattern_str, re.IGNORECASE)
                    for match in pattern.finditer(text):
                        entities.append({
                            "type": name.upper(),
                            "value": match.group(),
                            "start": match.start(),
                            "end": match.end(),
                            "confidence": 0.90,
                            "detector": "custom",
                        })
                except re.error:
                    pass  # Skip invalid patterns

        # Sort by position
        entities.sort(key=lambda e: e["start"])

        return entities
