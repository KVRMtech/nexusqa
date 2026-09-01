"""
Hands Engine — Rate Test Data Generator.

Generates comprehensive test data for rate table validation,
covering every combination of age, gender, tobacco status,
health class, face amount, and jurisdiction.
"""

from __future__ import annotations

from typing import Optional

from app.enums import Gender, HealthClass, TobaccoStatus, ProductType
from app.constants import AGE_BANDS

from .profile import SyntheticProfileGenerator


class RateTestDataGenerator:
    """Generates test data specifically for rate table validation."""

    def __init__(self, seed: Optional[int] = None):
        self.profile_gen = SyntheticProfileGenerator(seed)

    def generate(
        self,
        product_type: ProductType,
        jurisdictions: list[str],
        face_amounts: list[int],
        include_boundary_ages: bool = True,
        include_all_health_classes: bool = True,
        include_tobacco_variants: bool = True,
    ) -> list[dict]:
        """Generate comprehensive rate test data."""
        records: list[dict] = []

        # Determine ages to test
        if include_boundary_ages:
            ages: list[int] = []
            for band in AGE_BANDS:
                ages.extend([band["min"], band["max"]])
                mid = (band["min"] + band["max"]) // 2
                ages.append(mid)
            ages = sorted(set(ages))
        else:
            ages = [25, 35, 45, 55, 65]

        # Health classes
        if include_all_health_classes:
            health_classes = list(HealthClass)
        else:
            health_classes = [HealthClass.PREFERRED, HealthClass.STANDARD, HealthClass.SUBSTANDARD_A]

        # Tobacco
        if include_tobacco_variants:
            tobacco_statuses = list(TobaccoStatus)
        else:
            tobacco_statuses = [TobaccoStatus.NON_SMOKER, TobaccoStatus.SMOKER]

        # Generate all combinations
        for state in jurisdictions:
            for age in ages:
                for gender in [Gender.MALE, Gender.FEMALE]:
                    for tobacco in tobacco_statuses:
                        for hc in health_classes:
                            for face in face_amounts:
                                profile = self.profile_gen.generate_profile(
                                    product_type=product_type,
                                    jurisdiction=state,
                                    age_override=age,
                                    gender_override=gender,
                                    tobacco_override=tobacco,
                                    health_class_override=hc,
                                )
                                profile["policy"]["face_amount"] = face
                                profile["rate_test_key"] = (
                                    f"{state}_{age}_{gender.value}_{tobacco.value}_"
                                    f"{hc.value}_{face}"
                                )
                                records.append(profile)

        return records
