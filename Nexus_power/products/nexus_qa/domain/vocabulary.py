"""
Insurance Vocabulary Extension for Ears Engine.

Extracted from engines/ears-engine/main.py INSURANCE_VOCABULARY.
This is the complete insurance industry vocabulary that boosts
Whisper transcription accuracy for domain-specific terms.
"""

from __future__ import annotations

from nexus_sdk.plugins.extensions import VocabularyExtension, VocabularyTerm


def build_vocabulary_extension() -> VocabularyExtension:
    """Build the insurance vocabulary extension for Ears engine."""
    return VocabularyExtension(
        domain="insurance",
        terms=[
            # ── Life Insurance ────────────────────────────────
            VocabularyTerm(term="premium", boost_weight=1.5, definition="Amount paid for insurance coverage"),
            VocabularyTerm(term="beneficiary", boost_weight=1.5, definition="Person designated to receive policy benefits"),
            VocabularyTerm(term="annuitant", boost_weight=2.0, definition="Person receiving annuity payments"),
            VocabularyTerm(term="policyholder", boost_weight=1.5, aliases=["policy holder", "insured"]),
            VocabularyTerm(term="underwriting", boost_weight=2.0, definition="Risk assessment process"),
            VocabularyTerm(term="mortality table", boost_weight=2.0, aliases=["mortality tables"]),
            VocabularyTerm(term="cash value", boost_weight=1.5),
            VocabularyTerm(term="surrender charge", boost_weight=2.0),
            VocabularyTerm(term="death benefit", boost_weight=1.5),
            VocabularyTerm(term="term life", boost_weight=1.5, aliases=["term insurance"]),
            VocabularyTerm(term="whole life", boost_weight=1.5, aliases=["whole-life"]),
            VocabularyTerm(term="universal life", boost_weight=1.5, aliases=["UL"]),
            VocabularyTerm(term="variable life", boost_weight=1.5, aliases=["VUL", "variable universal life"]),
            VocabularyTerm(term="guaranteed issue", boost_weight=2.0),
            VocabularyTerm(term="simplified issue", boost_weight=2.0),
            VocabularyTerm(term="fully underwritten", boost_weight=2.0),
            VocabularyTerm(term="face amount", boost_weight=1.5, definition="Policy death benefit amount"),
            VocabularyTerm(term="rider", boost_weight=1.5, definition="Additional coverage added to a policy"),
            VocabularyTerm(term="waiver of premium", boost_weight=2.0, aliases=["WOP"]),
            VocabularyTerm(term="accelerated death benefit", boost_weight=2.0, aliases=["ADB"]),
            VocabularyTerm(term="conversion privilege", boost_weight=2.0),
            VocabularyTerm(term="incontestability", boost_weight=2.0, aliases=["incontestability clause"]),
            VocabularyTerm(term="contestability period", boost_weight=2.0),
            VocabularyTerm(term="suicide clause", boost_weight=2.0),
            VocabularyTerm(term="grace period", boost_weight=1.5),
            VocabularyTerm(term="lapse", boost_weight=1.5, definition="Policy termination for non-payment"),
            VocabularyTerm(term="reinstatement", boost_weight=1.5),
            VocabularyTerm(term="nonforfeiture", boost_weight=2.5, aliases=["non-forfeiture", "non forfeiture"]),
            VocabularyTerm(term="reduced paid-up", boost_weight=2.0),
            VocabularyTerm(term="extended term", boost_weight=2.0),

            # ── P&C (Property & Casualty) ────────────────────
            VocabularyTerm(term="deductible", boost_weight=1.5),
            VocabularyTerm(term="coinsurance", boost_weight=2.0),
            VocabularyTerm(term="subrogation", boost_weight=2.5),
            VocabularyTerm(term="indemnity", boost_weight=2.0),
            VocabularyTerm(term="actual cash value", boost_weight=2.0, aliases=["ACV"]),
            VocabularyTerm(term="replacement cost", boost_weight=1.5),
            VocabularyTerm(term="occurrence", boost_weight=1.5),
            VocabularyTerm(term="claims-made", boost_weight=2.0, aliases=["claims made"]),
            VocabularyTerm(term="aggregate limit", boost_weight=2.0),
            VocabularyTerm(term="per occurrence limit", boost_weight=2.0),
            VocabularyTerm(term="combined single limit", boost_weight=2.0, aliases=["CSL"]),
            VocabularyTerm(term="bodily injury", boost_weight=1.5, aliases=["BI"]),
            VocabularyTerm(term="property damage", boost_weight=1.5, aliases=["PD"]),
            VocabularyTerm(term="personal injury", boost_weight=1.5),
            VocabularyTerm(term="advertising injury", boost_weight=2.0),
            VocabularyTerm(term="products liability", boost_weight=2.0),
            VocabularyTerm(term="completed operations", boost_weight=2.0),
            VocabularyTerm(term="additional insured", boost_weight=1.5),
            VocabularyTerm(term="named insured", boost_weight=1.5),
            VocabularyTerm(term="certificate of insurance", boost_weight=2.0, aliases=["COI"]),
            VocabularyTerm(term="declarations page", boost_weight=2.0, aliases=["dec page"]),
            VocabularyTerm(term="endorsement", boost_weight=1.5),
            VocabularyTerm(term="exclusion", boost_weight=1.5),
            VocabularyTerm(term="insuring agreement", boost_weight=2.0),

            # ── Actuarial / Financial ────────────────────────
            VocabularyTerm(term="loss ratio", boost_weight=2.0),
            VocabularyTerm(term="combined ratio", boost_weight=2.0),
            VocabularyTerm(term="expense ratio", boost_weight=2.0),
            VocabularyTerm(term="IBNR", boost_weight=2.5, aliases=["incurred but not reported"]),
            VocabularyTerm(term="loss development factor", boost_weight=2.5),
            VocabularyTerm(term="credibility", boost_weight=1.5),
            VocabularyTerm(term="experience modification", boost_weight=2.0, aliases=["experience mod"]),
            VocabularyTerm(term="retrospective rating", boost_weight=2.5),
            VocabularyTerm(term="prospective rating", boost_weight=2.5),
            VocabularyTerm(term="catastrophe load", boost_weight=2.5),
            VocabularyTerm(term="reinsurance", boost_weight=2.0),
            VocabularyTerm(term="treaty reinsurance", boost_weight=2.5),
            VocabularyTerm(term="facultative reinsurance", boost_weight=2.5),
            VocabularyTerm(term="ceding company", boost_weight=2.5),
            VocabularyTerm(term="CSO", boost_weight=2.5, aliases=["CSO table", "2017 CSO", "commissioners standard ordinary"]),
            VocabularyTerm(term="CIDA", boost_weight=2.5, aliases=["CIDA table", "85 CIDA"]),
            VocabularyTerm(term="net premium", boost_weight=2.0, aliases=["modified net premium"]),

            # ── Regulatory / Compliance ──────────────────────
            VocabularyTerm(term="NAIC", boost_weight=2.5, aliases=["National Association of Insurance Commissioners"]),
            VocabularyTerm(term="SERFF", boost_weight=2.5, aliases=["System for Electronic Rates & Forms Filing"]),
            VocabularyTerm(term="rate filing", boost_weight=2.0, aliases=["rate filings"]),
            VocabularyTerm(term="form filing", boost_weight=2.0, aliases=["form filings"]),
            VocabularyTerm(term="market conduct", boost_weight=2.0),
            VocabularyTerm(term="MIB", boost_weight=2.5, aliases=["Medical Information Bureau"]),
            VocabularyTerm(term="CLUE report", boost_weight=2.5, aliases=["Comprehensive Loss Underwriting Exchange"]),
            VocabularyTerm(term="MVR", boost_weight=2.5, aliases=["motor vehicle record"]),
            VocabularyTerm(term="state filing", boost_weight=1.5),
            VocabularyTerm(term="admitted carrier", boost_weight=2.0),
            VocabularyTerm(term="surplus lines", boost_weight=2.0),
            VocabularyTerm(term="MGA", boost_weight=2.0, aliases=["managing general agent"]),
            VocabularyTerm(term="NPN", boost_weight=2.5, aliases=["National Producer Number"]),
            VocabularyTerm(term="producer code", boost_weight=1.5),
            VocabularyTerm(term="commission schedule", boost_weight=1.5),
            VocabularyTerm(term="TDI", boost_weight=2.5, aliases=["Texas Department of Insurance"]),
            VocabularyTerm(term="DOI", boost_weight=2.5, aliases=["Department of Insurance"]),

            # ── IUL / Indexed Products ───────────────────────
            VocabularyTerm(term="IUL", boost_weight=2.5, aliases=["indexed universal life"]),
            VocabularyTerm(term="cap rate", boost_weight=2.0),
            VocabularyTerm(term="floor rate", boost_weight=2.0),
            VocabularyTerm(term="participation rate", boost_weight=2.0),
            VocabularyTerm(term="index crediting", boost_weight=2.0),
            VocabularyTerm(term="policy loan", boost_weight=1.5),
            VocabularyTerm(term="cost of insurance", boost_weight=2.0, aliases=["COI"]),
        ],
        boost_phrases=[
            "knowledge transfer",
            "business rule",
            "acceptance criteria",
            "test case",
            "rate table",
            "policy administration",
            "claims processing",
            "underwriting guidelines",
            "compliance requirement",
            "state regulation",
            "product filing",
            "actuarial memorandum",
        ],
        suppressed_phrases=[
            # Common Whisper misrecognitions in insurance context
            "non-for-feature",   # → nonforfeiture
            "see so table",      # → CSO table
            "sea so",            # → CSO
            "end A I C",         # → NAIC
        ],
    )
