"""
Hands Engine — Synthetic Profile Generator.

Generates PII-safe synthetic insurance applicant profiles with
full insurance-specific attributes: applicant data, policy info,
beneficiary, regulatory context, and test metadata.
"""

from __future__ import annotations

import uuid
import random
from datetime import date
from typing import Optional

from app.enums import (
    Gender, TobaccoStatus, HealthClass, ProductType, RiderType, DataStrategy,
)
from app.constants import (
    US_STATES, AGE_BANDS, FACE_AMOUNTS, PAYMENT_MODES,
    SYNTHETIC_FIRST_NAMES, SYNTHETIC_LAST_NAMES,
)


class SyntheticProfileGenerator:
    """Generates PII-safe synthetic insurance applicant profiles."""

    def __init__(self, seed: Optional[int] = None):
        self.rng = random.Random(seed)
        self._sequence = 0

    def generate_profile(
        self,
        product_type: Optional[ProductType] = None,
        jurisdiction: Optional[str] = None,
        age_override: Optional[int] = None,
        gender_override: Optional[Gender] = None,
        tobacco_override: Optional[TobaccoStatus] = None,
        health_class_override: Optional[HealthClass] = None,
    ) -> dict:
        """Generate one synthetic applicant profile."""
        self._sequence += 1

        # Select attributes — use `is not None` to avoid falsy-value bugs
        # (e.g. age_override=0, tobacco_override=False are valid overrides)
        age = age_override if age_override is not None else self._random_age(product_type)
        gender = gender_override if gender_override is not None else self.rng.choice(list(Gender))
        state = jurisdiction if jurisdiction is not None else self.rng.choice(list(US_STATES.keys()))
        tobacco = tobacco_override if tobacco_override is not None else self._random_tobacco(age)
        health_class = health_class_override if health_class_override is not None else self._random_health_class(age, tobacco)
        product = product_type if product_type is not None else self._random_product(age)

        # Generate synthetic identity
        first_name = self.rng.choice(SYNTHETIC_FIRST_NAMES)
        last_name = self.rng.choice(SYNTHETIC_LAST_NAMES)
        dob = self._age_to_dob(age)

        # Synthetic SSN (900-xx-xxxx series — reserved for testing by SSA)
        ssn = f"900-{self.rng.randint(10, 99)}-{self.rng.randint(1000, 9999)}"

        # Synthetic contact info
        email = f"{first_name.lower()}.{last_name.lower()}.{self._sequence}@synthetic-nexus.test"
        phone = f"555-{self.rng.randint(100, 999)}-{self.rng.randint(1000, 9999)}"
        address = self._generate_address(state)

        # Face amount
        face_amount = self._random_face_amount(product)

        # Riders
        riders = self._select_riders(product, age) if product else []

        # Beneficiary
        beneficiary = self._generate_beneficiary()

        profile = {
            "record_id": str(uuid.uuid4()),
            "synthetic": True,  # WATERMARK: always True
            "sequence_number": self._sequence,

            # Applicant
            "applicant": {
                "first_name": first_name,
                "last_name": last_name,
                "date_of_birth": dob.isoformat(),
                "age": age,
                "gender": gender.value,
                "ssn": ssn,
                "email": email,
                "phone": phone,
                "address": address,
                "tobacco_status": tobacco.value,
                "health_class": health_class.value,
            },

            # Policy
            "policy": {
                "product_type": product.value if product else None,
                "jurisdiction": state,
                "jurisdiction_name": US_STATES.get(state, {}).get("name", state),
                "face_amount": face_amount,
                "payment_mode": self.rng.choice(PAYMENT_MODES),
                "riders": [r.value for r in riders],
            },

            # Beneficiary
            "beneficiary": beneficiary,

            # Regulatory
            "regulatory": {
                "free_look_days": US_STATES.get(state, {}).get("free_look_days", 10),
                "state_approval_required": state in ("NY", "CA", "CT", "MA"),
            },

            # Test metadata
            "test_metadata": {
                "age_band": self._get_age_band(age),
                "risk_tier": self._compute_risk_tier(age, tobacco, health_class),
                "boundary_flags": self._check_boundary_flags(age, face_amount),
            },
        }

        return profile

    def generate_batch(
        self,
        count: int,
        product_type: Optional[ProductType] = None,
        jurisdictions: Optional[list[str]] = None,
        age_range: Optional[dict] = None,
        strategy: DataStrategy = DataStrategy.RANDOM,
    ) -> list[dict]:
        """Generate a batch of profiles using the specified strategy."""
        profiles: list[dict] = []

        if strategy == DataStrategy.RANDOM:
            for _ in range(count):
                jurisdiction = self.rng.choice(jurisdictions) if jurisdictions else None
                age = None
                if age_range:
                    age = self.rng.randint(age_range.get("min", 0), age_range.get("max", 99))
                profiles.append(self.generate_profile(
                    product_type=product_type,
                    jurisdiction=jurisdiction,
                    age_override=age,
                ))

        elif strategy == DataStrategy.BOUNDARY:
            # Generate profiles at every age band boundary
            boundary_ages: list[int] = []
            for band in AGE_BANDS:
                boundary_ages.extend([
                    band["min"],
                    band["min"] + 1 if band["min"] > 0 else 0,
                    band["max"] - 1 if band["max"] < 99 else 99,
                    band["max"],
                ])
            boundary_ages = sorted(set(boundary_ages))

            states = jurisdictions or list(US_STATES.keys())
            for age in boundary_ages:
                for state in states[:min(len(states), max(1, count // len(boundary_ages)))]:
                    for tobacco in [TobaccoStatus.NON_SMOKER, TobaccoStatus.SMOKER]:
                        profiles.append(self.generate_profile(
                            product_type=product_type,
                            jurisdiction=state,
                            age_override=age,
                            tobacco_override=tobacco,
                        ))
                        if len(profiles) >= count:
                            return profiles[:count]

        elif strategy == DataStrategy.EQUIVALENCE:
            # One representative from each equivalence class
            genders = list(Gender)
            tobacco_statuses = list(TobaccoStatus)
            health_classes = [HealthClass.PREFERRED_PLUS, HealthClass.STANDARD, HealthClass.SUBSTANDARD_A]
            states = jurisdictions or ["NY", "CA", "TX", "FL", "IL", "OH", "PA", "GA"]
            representative_ages = [5, 22, 30, 40, 50, 60, 70, 85]

            for age in representative_ages:
                for gender in genders:
                    for tobacco in tobacco_statuses:
                        for hc in health_classes:
                            state = self.rng.choice(states)
                            profiles.append(self.generate_profile(
                                product_type=product_type,
                                jurisdiction=state,
                                age_override=age,
                                gender_override=gender,
                                tobacco_override=tobacco,
                                health_class_override=hc,
                            ))
                            if len(profiles) >= count:
                                return profiles[:count]

        elif strategy == DataStrategy.RISK_FOCUSED:
            # Prioritize high-risk combinations
            high_risk_ages = [0, 1, 17, 18, 64, 65, 75, 85, 99]
            high_risk_states = ["NY", "CA", "CT", "MA"]  # Heavy regulation
            for age in high_risk_ages:
                for state in high_risk_states:
                    for tobacco in [TobaccoStatus.SMOKER, TobaccoStatus.FORMER_SMOKER]:
                        profiles.append(self.generate_profile(
                            product_type=product_type,
                            jurisdiction=state,
                            age_override=age,
                            tobacco_override=tobacco,
                        ))
                        if len(profiles) >= count:
                            return profiles[:count]
            # Fill remaining with random
            while len(profiles) < count:
                profiles.append(self.generate_profile(product_type=product_type))

        return profiles[:count]

    # ── Private helpers ────────────────────────────────────────

    def _random_age(self, product: Optional[ProductType] = None) -> int:
        """Generate age appropriate for the product."""
        if product in (ProductType.FINAL_EXPENSE,):
            return self.rng.randint(50, 85)
        elif product in (ProductType.ANNUITY_FIXED, ProductType.ANNUITY_VARIABLE, ProductType.ANNUITY_INDEXED):
            return self.rng.randint(30, 80)
        elif product in (ProductType.TERM_10, ProductType.TERM_15, ProductType.TERM_20, ProductType.TERM_30):
            return self.rng.randint(18, 70)
        else:
            return self.rng.randint(0, 99)

    def _random_tobacco(self, age: int) -> TobaccoStatus:
        if age < 18:
            return TobaccoStatus.NON_SMOKER
        weights = [0.70, 0.15, 0.15]
        return self.rng.choices(list(TobaccoStatus), weights=weights, k=1)[0]

    def _random_health_class(self, age: int, tobacco: TobaccoStatus) -> HealthClass:
        if age < 18:
            return HealthClass.STANDARD
        if tobacco == TobaccoStatus.SMOKER:
            weights = [0.02, 0.08, 0.15, 0.45, 0.20, 0.08, 0.02]
        elif tobacco == TobaccoStatus.FORMER_SMOKER:
            weights = [0.05, 0.15, 0.25, 0.35, 0.12, 0.06, 0.02]
        else:
            weights = [0.15, 0.25, 0.30, 0.20, 0.06, 0.03, 0.01]
        return self.rng.choices(list(HealthClass), weights=weights, k=1)[0]

    def _random_product(self, age: int) -> ProductType:
        if age < 18:
            return self.rng.choice([ProductType.WHOLE_LIFE, ProductType.TERM_20])
        elif age > 75:
            return self.rng.choice([ProductType.FINAL_EXPENSE, ProductType.WHOLE_LIFE])
        else:
            return self.rng.choice(list(ProductType))

    def _random_face_amount(self, product: Optional[ProductType]) -> int:
        if product == ProductType.FINAL_EXPENSE:
            return self.rng.choice([5_000, 10_000, 15_000, 20_000, 25_000])
        elif product in (ProductType.TERM_10, ProductType.TERM_15, ProductType.TERM_20, ProductType.TERM_30):
            return self.rng.choice([100_000, 250_000, 500_000, 750_000, 1_000_000, 2_000_000])
        else:
            return self.rng.choice(FACE_AMOUNTS)

    def _age_to_dob(self, age: int) -> date:
        today = date.today()
        birth_year = today.year - age
        birth_month = self.rng.randint(1, 12)
        birth_day = self.rng.randint(1, 28)
        return date(birth_year, birth_month, birth_day)

    def _generate_address(self, state: str) -> dict:
        street_num = self.rng.randint(100, 9999)
        streets = ["Test Ave", "Synthetic Blvd", "QA Lane", "Nexus Drive", "Automation Way"]
        cities = {
            "NY": "Test York", "CA": "San Testino", "TX": "Testston",
            "FL": "Synthville", "IL": "Testcago",
        }
        return {
            "street": f"{street_num} {self.rng.choice(streets)}",
            "city": cities.get(state, f"TestCity-{state}"),
            "state": state,
            "zip": f"{self.rng.randint(10000, 99999)}",
        }

    def _generate_beneficiary(self) -> dict:
        bene_type = self.rng.choice(["individual", "trust", "estate", "charity"])
        if bene_type == "individual":
            return {
                "type": "individual",
                "name": f"{self.rng.choice(SYNTHETIC_FIRST_NAMES)} {self.rng.choice(SYNTHETIC_LAST_NAMES)}",
                "relationship": self.rng.choice(["spouse", "child", "parent", "sibling", "other"]),
                "percentage": 100,
            }
        elif bene_type == "trust":
            return {
                "type": "trust",
                "name": f"Synthetic Family Trust #{self.rng.randint(1000, 9999)}",
                "percentage": 100,
            }
        elif bene_type == "estate":
            return {"type": "estate", "name": "Estate of Insured", "percentage": 100}
        else:
            return {
                "type": "charity",
                "name": f"Synthetic Charity Foundation #{self.rng.randint(100, 999)}",
                "percentage": 100,
            }

    def _select_riders(self, product: ProductType, age: int) -> list[RiderType]:
        available = list(RiderType)
        if age >= 65:
            available = [r for r in available if r != RiderType.CHILD_TERM]
        if product in (ProductType.TERM_10, ProductType.TERM_15):
            available = [r for r in available if r not in (RiderType.LONG_TERM_CARE, RiderType.RETURN_OF_PREMIUM)]
        count = self.rng.randint(0, min(3, len(available)))
        return self.rng.sample(available, count)

    def _get_age_band(self, age: int) -> str:
        for band in AGE_BANDS:
            if band["min"] <= age <= band["max"]:
                return band["label"]
        return "unknown"

    def _compute_risk_tier(self, age: int, tobacco: TobaccoStatus, health_class: HealthClass) -> str:
        risk_score = 0
        if age < 18 or age > 70:
            risk_score += 2
        if tobacco == TobaccoStatus.SMOKER:
            risk_score += 3
        elif tobacco == TobaccoStatus.FORMER_SMOKER:
            risk_score += 1
        if health_class in (HealthClass.SUBSTANDARD_A, HealthClass.SUBSTANDARD_B, HealthClass.DECLINE):
            risk_score += 3
        elif health_class == HealthClass.STANDARD:
            risk_score += 1

        if risk_score >= 5:
            return "high_risk"
        elif risk_score >= 3:
            return "elevated_risk"
        else:
            return "standard_risk"

    def _check_boundary_flags(self, age: int, face_amount: int) -> list[str]:
        flags: list[str] = []
        for band in AGE_BANDS:
            if age == band["min"]:
                flags.append(f"age_band_lower_{band['label']}")
            if age == band["max"]:
                flags.append(f"age_band_upper_{band['label']}")
        # Face amount boundaries
        boundaries = [25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000]
        for b in boundaries:
            if face_amount == b - 1:
                flags.append(f"just_below_{b}")
            elif face_amount == b:
                flags.append(f"at_boundary_{b}")
            elif face_amount == b + 1:
                flags.append(f"just_above_{b}")
        return flags
