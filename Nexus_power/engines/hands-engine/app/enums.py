"""
Hands Engine — Insurance Domain Enums.

All enumerations used by the synthetic test data generators.
"""

from enum import Enum


class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    NON_BINARY = "non_binary"


class TobaccoStatus(str, Enum):
    NON_SMOKER = "non_smoker"
    SMOKER = "smoker"
    FORMER_SMOKER = "former_smoker"


class HealthClass(str, Enum):
    PREFERRED_PLUS = "preferred_plus"
    PREFERRED = "preferred"
    STANDARD_PLUS = "standard_plus"
    STANDARD = "standard"
    SUBSTANDARD_A = "substandard_a"
    SUBSTANDARD_B = "substandard_b"
    DECLINE = "decline"


class ProductType(str, Enum):
    TERM_10 = "term_10"
    TERM_15 = "term_15"
    TERM_20 = "term_20"
    TERM_30 = "term_30"
    WHOLE_LIFE = "whole_life"
    UNIVERSAL_LIFE = "universal_life"
    VARIABLE_UL = "variable_universal_life"
    INDEXED_UL = "indexed_universal_life"
    FINAL_EXPENSE = "final_expense"
    ANNUITY_FIXED = "annuity_fixed"
    ANNUITY_VARIABLE = "annuity_variable"
    ANNUITY_INDEXED = "annuity_indexed"


class RiderType(str, Enum):
    WAIVER_OF_PREMIUM = "wop"
    ACCIDENTAL_DEATH = "adb"
    GUARANTEED_PURCHASE = "gpo"
    CRITICAL_ILLNESS = "cir"
    COLA = "cola"
    CHILD_TERM = "child_term"
    SPOUSE_TERM = "spouse_term"
    LONG_TERM_CARE = "ltc"
    RETURN_OF_PREMIUM = "rop"
    DISABILITY_INCOME = "disability_income"


class DataStrategy(str, Enum):
    """How to generate the test data."""
    RANDOM = "random"
    BOUNDARY = "boundary"
    EQUIVALENCE = "equivalence"
    PAIRWISE = "pairwise"
    FULL_COMBINATORIAL = "full_combinatorial"
    RISK_FOCUSED = "risk_focused"
