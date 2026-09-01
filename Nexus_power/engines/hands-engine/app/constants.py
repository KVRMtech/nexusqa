"""
Hands Engine — Insurance Domain Constants.

US state regulatory data, age bands, face amounts, payment modes,
and synthetic PII-safe names used by all generators.
"""

# ─── US States + Insurance Regulatory Data ─────────────────────

US_STATES = {
    "AL": {"name": "Alabama", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "AK": {"name": "Alaska", "min_age_life": 0, "max_age_life": 99, "free_look_days": 20},
    "AZ": {"name": "Arizona", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "AR": {"name": "Arkansas", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "CA": {"name": "California", "min_age_life": 0, "max_age_life": 99, "free_look_days": 30},
    "CO": {"name": "Colorado", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "CT": {"name": "Connecticut", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "DE": {"name": "Delaware", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "FL": {"name": "Florida", "min_age_life": 0, "max_age_life": 99, "free_look_days": 14},
    "GA": {"name": "Georgia", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "HI": {"name": "Hawaii", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "ID": {"name": "Idaho", "min_age_life": 0, "max_age_life": 99, "free_look_days": 20},
    "IL": {"name": "Illinois", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "IN": {"name": "Indiana", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "IA": {"name": "Iowa", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "KS": {"name": "Kansas", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "KY": {"name": "Kentucky", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "LA": {"name": "Louisiana", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "ME": {"name": "Maine", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "MD": {"name": "Maryland", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "MA": {"name": "Massachusetts", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "MI": {"name": "Michigan", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "MN": {"name": "Minnesota", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "MS": {"name": "Mississippi", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "MO": {"name": "Missouri", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "MT": {"name": "Montana", "min_age_life": 0, "max_age_life": 99, "free_look_days": 20},
    "NE": {"name": "Nebraska", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "NV": {"name": "Nevada", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "NH": {"name": "New Hampshire", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "NJ": {"name": "New Jersey", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "NM": {"name": "New Mexico", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "NY": {"name": "New York", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "NC": {"name": "North Carolina", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "ND": {"name": "North Dakota", "min_age_life": 0, "max_age_life": 99, "free_look_days": 20},
    "OH": {"name": "Ohio", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "OK": {"name": "Oklahoma", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "OR": {"name": "Oregon", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "PA": {"name": "Pennsylvania", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "RI": {"name": "Rhode Island", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "SC": {"name": "South Carolina", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "SD": {"name": "South Dakota", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "TN": {"name": "Tennessee", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "TX": {"name": "Texas", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "UT": {"name": "Utah", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "VT": {"name": "Vermont", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "VA": {"name": "Virginia", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "WA": {"name": "Washington", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "WV": {"name": "West Virginia", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "WI": {"name": "Wisconsin", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "WY": {"name": "Wyoming", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
    "DC": {"name": "District of Columbia", "min_age_life": 0, "max_age_life": 99, "free_look_days": 10},
}

# Standard age bands for Life Insurance testing
AGE_BANDS = [
    {"label": "juvenile", "min": 0, "max": 17},
    {"label": "young_adult", "min": 18, "max": 25},
    {"label": "adult_26_35", "min": 26, "max": 35},
    {"label": "adult_36_45", "min": 36, "max": 45},
    {"label": "adult_46_55", "min": 46, "max": 55},
    {"label": "adult_56_65", "min": 56, "max": 65},
    {"label": "senior_66_75", "min": 66, "max": 75},
    {"label": "senior_76_plus", "min": 76, "max": 99},
]

# Face amounts with boundary values
FACE_AMOUNTS = [
    1_000, 5_000, 10_000, 24_999, 25_000, 25_001,
    50_000, 99_999, 100_000, 100_001,
    250_000, 499_999, 500_000, 500_001,
    1_000_000, 2_000_000, 5_000_000, 10_000_000,
]

# Premium payment frequencies
PAYMENT_MODES = ["annual", "semi_annual", "quarterly", "monthly", "monthly_eft"]

# Synthetic first & last names (no real PII)
SYNTHETIC_FIRST_NAMES = [
    "TestAlpha", "TestBravo", "TestCharlie", "TestDelta", "TestEcho",
    "TestFoxtrot", "TestGolf", "TestHotel", "TestIndia", "TestJuliet",
    "TestKilo", "TestLima", "TestMike", "TestNovember", "TestOscar",
    "TestPapa", "TestQuebec", "TestRomeo", "TestSierra", "TestTango",
]
SYNTHETIC_LAST_NAMES = [
    "SynthOne", "SynthTwo", "SynthThree", "SynthFour", "SynthFive",
    "SynthSix", "SynthSeven", "SynthEight", "SynthNine", "SynthTen",
    "SynthEleven", "SynthTwelve", "SynthThirteen", "SynthFourteen", "SynthFifteen",
]
