"""
Hands Engine — Unit tests.

Tests all 5 generators + all 6 enums. Pure logic, no I/O.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "hands-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Enums ────────────────────────────────────────────────────


class TestGenderEnum:
    def test_values(self):
        from main import Gender
        assert Gender.MALE == "male"
        assert Gender.FEMALE == "female"
        assert Gender.NON_BINARY == "non_binary"
        assert len(Gender) == 3


class TestTobaccoStatusEnum:
    def test_values(self):
        from main import TobaccoStatus
        assert TobaccoStatus.NON_SMOKER == "non_smoker"
        assert TobaccoStatus.SMOKER == "smoker"
        assert TobaccoStatus.FORMER_SMOKER == "former_smoker"
        assert len(TobaccoStatus) == 3


class TestHealthClassEnum:
    def test_values(self):
        from main import HealthClass
        assert HealthClass.PREFERRED_PLUS == "preferred_plus"
        assert HealthClass.DECLINE == "decline"
        assert len(HealthClass) == 7


class TestProductTypeEnum:
    def test_values(self):
        from main import ProductType
        assert ProductType.TERM_10 == "term_10"
        assert ProductType.WHOLE_LIFE == "whole_life"
        assert ProductType.ANNUITY_INDEXED == "annuity_indexed"
        assert len(ProductType) == 12


class TestRiderTypeEnum:
    def test_values(self):
        from main import RiderType
        assert RiderType.WAIVER_OF_PREMIUM == "wop"
        assert RiderType.ACCIDENTAL_DEATH == "adb"
        assert RiderType.RETURN_OF_PREMIUM == "rop"
        assert len(RiderType) == 10


class TestDataStrategyEnum:
    def test_values(self):
        from main import DataStrategy
        assert DataStrategy.RANDOM == "random"
        assert DataStrategy.BOUNDARY == "boundary"
        assert DataStrategy.PAIRWISE == "pairwise"
        assert DataStrategy.FULL_COMBINATORIAL == "full_combinatorial"
        assert DataStrategy.RISK_FOCUSED == "risk_focused"
        assert len(DataStrategy) == 6


# ─── SyntheticProfileGenerator ─────────────────────────────────


class TestSyntheticProfileGenerator:

    def setup_method(self):
        from main import SyntheticProfileGenerator
        self.gen = SyntheticProfileGenerator(seed=42)

    def test_generate_single_profile(self):
        profile = self.gen.generate_profile()
        assert profile["synthetic"] is True
        assert "applicant" in profile
        assert "policy" in profile
        assert "beneficiary" in profile
        assert "test_metadata" in profile

    def test_profile_has_required_applicant_fields(self):
        profile = self.gen.generate_profile()
        app = profile["applicant"]
        for field in ["first_name", "last_name", "date_of_birth", "age",
                       "gender", "ssn", "email", "phone", "address",
                       "tobacco_status", "health_class"]:
            assert field in app, f"Missing field: {field}"

    def test_ssn_starts_with_900(self):
        profile = self.gen.generate_profile()
        assert profile["applicant"]["ssn"].startswith("900-")

    def test_email_is_synthetic(self):
        profile = self.gen.generate_profile()
        assert "@synthetic-nexus.test" in profile["applicant"]["email"]

    def test_seed_deterministic(self):
        from main import SyntheticProfileGenerator
        gen1 = SyntheticProfileGenerator(seed=99)
        gen2 = SyntheticProfileGenerator(seed=99)
        p1 = gen1.generate_profile()
        p2 = gen2.generate_profile()
        assert p1["applicant"]["first_name"] == p2["applicant"]["first_name"]
        assert p1["applicant"]["age"] == p2["applicant"]["age"]

    def test_age_override(self):
        profile = self.gen.generate_profile(age_override=35)
        assert profile["applicant"]["age"] == 35

    def test_gender_override(self):
        from main import Gender
        profile = self.gen.generate_profile(gender_override=Gender.FEMALE)
        assert profile["applicant"]["gender"] == "female"

    def test_tobacco_override(self):
        from main import TobaccoStatus
        profile = self.gen.generate_profile(tobacco_override=TobaccoStatus.SMOKER)
        assert profile["applicant"]["tobacco_status"] == "smoker"

    def test_jurisdiction_override(self):
        profile = self.gen.generate_profile(jurisdiction="NY")
        assert profile["policy"]["jurisdiction"] == "NY"
        assert profile["policy"]["jurisdiction_name"] == "New York"

    def test_product_type_override(self):
        from main import ProductType
        profile = self.gen.generate_profile(product_type=ProductType.TERM_20)
        assert profile["policy"]["product_type"] == "term_20"

    def test_unique_record_ids(self):
        p1 = self.gen.generate_profile()
        p2 = self.gen.generate_profile()
        assert p1["record_id"] != p2["record_id"]

    def test_age_band_metadata(self):
        profile = self.gen.generate_profile(age_override=30)
        assert profile["test_metadata"]["age_band"] == "adult_26_35"

    def test_juvenile_no_tobacco(self):
        profile = self.gen.generate_profile(age_override=10)
        assert profile["applicant"]["tobacco_status"] == "non_smoker"

    def test_boundary_flags_at_boundary(self):
        profile = self.gen.generate_profile(age_override=18)
        flags = profile["test_metadata"]["boundary_flags"]
        assert any("age_band_lower" in f for f in flags)


class TestSyntheticProfileGeneratorBatch:

    def setup_method(self):
        from main import SyntheticProfileGenerator
        self.gen = SyntheticProfileGenerator(seed=42)

    def test_random_batch(self):
        from main import DataStrategy
        profiles = self.gen.generate_batch(20, strategy=DataStrategy.RANDOM)
        assert len(profiles) == 20
        assert all(p["synthetic"] for p in profiles)

    def test_boundary_batch(self):
        from main import DataStrategy
        profiles = self.gen.generate_batch(50, strategy=DataStrategy.BOUNDARY)
        assert len(profiles) <= 50
        ages = {p["applicant"]["age"] for p in profiles}
        # Boundary strategy should hit age band edges
        assert 0 in ages or 17 in ages or 18 in ages

    def test_equivalence_batch(self):
        from main import DataStrategy
        profiles = self.gen.generate_batch(50, strategy=DataStrategy.EQUIVALENCE)
        assert len(profiles) <= 50
        genders = {p["applicant"]["gender"] for p in profiles}
        assert len(genders) >= 2

    def test_risk_focused_batch(self):
        from main import DataStrategy
        profiles = self.gen.generate_batch(30, strategy=DataStrategy.RISK_FOCUSED)
        assert len(profiles) <= 30
        # Should include high-risk ages
        ages = {p["applicant"]["age"] for p in profiles}
        assert ages & {0, 1, 17, 18, 99}  # At least one high-risk age

    def test_batch_respects_count_limit(self):
        from main import DataStrategy
        profiles = self.gen.generate_batch(5, strategy=DataStrategy.RANDOM)
        assert len(profiles) == 5

    def test_batch_with_jurisdiction_filter(self):
        from main import DataStrategy
        profiles = self.gen.generate_batch(
            10, strategy=DataStrategy.RANDOM, jurisdictions=["NY", "CA"],
        )
        states = {p["policy"]["jurisdiction"] for p in profiles}
        assert states.issubset({"NY", "CA"})


# ─── CombinatorialGenerator ───────────────────────────────────


class TestCombinatorialGenerator:

    def test_pairwise_basic(self):
        from main import CombinatorialGenerator
        dims = {"gender": ["M", "F"], "tobacco": ["Y", "N"], "age_band": ["young", "mid", "senior"]}
        result = CombinatorialGenerator.generate_pairwise(dims, max_combinations=100)
        assert len(result) > 0
        # Verify every pair is covered at least once
        all_pairs = set()
        keys = list(dims.keys())
        for row in result:
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    all_pairs.add((keys[i], str(row[keys[i]]), keys[j], str(row[keys[j]])))
        # Every (dim_i value, dim_j value) pair should exist
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                for vi in dims[keys[i]]:
                    for vj in dims[keys[j]]:
                        assert (keys[i], str(vi), keys[j], str(vj)) in all_pairs

    def test_pairwise_single_dimension(self):
        from main import CombinatorialGenerator
        dims = {"color": ["red", "blue", "green"]}
        result = CombinatorialGenerator.generate_pairwise(dims, max_combinations=10)
        assert len(result) == 3

    def test_full_combinatorial(self):
        from main import CombinatorialGenerator
        dims = {"a": [1, 2], "b": ["x", "y"]}
        result = CombinatorialGenerator.generate_full(dims, max_combinations=100)
        assert len(result) == 4  # 2 × 2

    def test_full_with_constraint(self):
        from main import CombinatorialGenerator
        dims = {"age": [10, 25, 50], "product": ["whole_life", "term_20"]}
        constraints = [{"if": {"age": "<18"}, "then": {"product": "!= whole_life"}}]
        result = CombinatorialGenerator.generate_full(dims, constraints=constraints)
        for row in result:
            if row["age"] < 18:
                assert row["product"] != "whole_life"

    def test_full_respects_max(self):
        from main import CombinatorialGenerator
        dims = {"a": list(range(10)), "b": list(range(10)), "c": list(range(10))}
        result = CombinatorialGenerator.generate_full(dims, max_combinations=50)
        assert len(result) <= 50


# ─── BoundaryValueGenerator ───────────────────────────────────


class TestBoundaryValueGenerator:

    def test_numeric_basic(self):
        from main import BoundaryValueGenerator
        bvs = BoundaryValueGenerator.generate("age", "numeric", min_value=0, max_value=99)
        assert len(bvs) > 0
        values = [b["value"] for b in bvs]
        assert 0 in values      # at min
        assert 99 in values     # at max
        assert 1 in values      # above min
        assert 98 in values     # below max

    def test_numeric_boundary_points(self):
        from main import BoundaryValueGenerator
        bvs = BoundaryValueGenerator.generate(
            "age", "numeric", min_value=0, max_value=99,
            boundary_points=[18, 65],
        )
        values = [b["value"] for b in bvs]
        assert 18 in values
        assert 17 in values     # below 18
        assert 19 in values     # above 18
        assert 65 in values
        assert 64 in values
        assert 66 in values

    def test_numeric_invalid_values(self):
        from main import BoundaryValueGenerator
        bvs = BoundaryValueGenerator.generate("age", "numeric", min_value=0, max_value=99, include_invalid=True)
        categories = {b["category"] for b in bvs}
        assert "far_below_min" in categories or "far_above_max" in categories
        assert "zero" in categories
        assert "negative" in categories

    def test_numeric_no_invalid(self):
        from main import BoundaryValueGenerator
        bvs = BoundaryValueGenerator.generate("age", "numeric", min_value=0, max_value=99, include_invalid=False)
        categories = {b["category"] for b in bvs}
        assert "far_below_min" not in categories
        assert "negative" not in categories

    def test_string_boundaries(self):
        from main import BoundaryValueGenerator
        bvs = BoundaryValueGenerator.generate("name", "string", min_value=1, max_value=50)
        categories = {b["category"] for b in bvs}
        assert "min_length" in categories
        assert "max_length" in categories
        assert "above_max_length" in categories

    def test_date_boundaries(self):
        from main import BoundaryValueGenerator
        bvs = BoundaryValueGenerator.generate(
            "dob", "date",
            min_value="2000-01-01",
            max_value="2024-12-31",
        )
        assert len(bvs) > 0
        categories = {b["category"] for b in bvs}
        assert "at_min" in categories
        assert "at_max" in categories


# ─── PolicyNumberGenerator ─────────────────────────────────────


class TestPolicyNumberGenerator:

    def test_basic_pattern(self):
        from main import PolicyNumberGenerator
        numbers = PolicyNumberGenerator.generate("POL-{STATE}-{YEAR}-{SEQ:06d}", count=5)
        assert len(numbers) == 5
        for n in numbers:
            assert n.startswith("POL-")
            parts = n.split("-")
            assert len(parts[1]) == 2  # 2-letter state
            assert len(parts[2]) == 4  # 4-digit year

    def test_sequence_increments(self):
        from main import PolicyNumberGenerator
        numbers = PolicyNumberGenerator.generate("SEQ-{SEQ:04d}", count=3, start_sequence=10)
        assert numbers[0].endswith("0010")
        assert numbers[1].endswith("0011")
        assert numbers[2].endswith("0012")

    def test_alpha_and_digit_tokens(self):
        from main import PolicyNumberGenerator
        numbers = PolicyNumberGenerator.generate("{ALPHA:3}-{DIGIT:4}", count=2)
        assert len(numbers) == 2
        for n in numbers:
            parts = n.split("-")
            assert parts[0].isalpha() and len(parts[0]) == 3
            assert parts[1].isdigit() and len(parts[1]) == 4

    def test_yy_token(self):
        from main import PolicyNumberGenerator
        from datetime import date
        numbers = PolicyNumberGenerator.generate("P-{YY}-{SEQ:03d}", count=1)
        expected_yy = str(date.today().year)[-2:]
        assert expected_yy in numbers[0]


# ─── RateTestDataGenerator ────────────────────────────────────


class TestRateTestDataGenerator:

    def setup_method(self):
        from main import RateTestDataGenerator
        self.gen = RateTestDataGenerator(seed=42)

    def test_generate_basic(self):
        from main import ProductType
        records = self.gen.generate(
            product_type=ProductType.TERM_20,
            jurisdictions=["NY"],
            face_amounts=[100_000],
            include_boundary_ages=False,
            include_all_health_classes=False,
            include_tobacco_variants=False,
        )
        assert len(records) > 0
        for r in records:
            assert r["policy"]["jurisdiction"] == "NY"
            assert r["policy"]["product_type"] == "term_20"
            assert "rate_test_key" in r

    def test_generate_with_boundary_ages(self):
        from main import ProductType
        records = self.gen.generate(
            product_type=ProductType.WHOLE_LIFE,
            jurisdictions=["CA"],
            face_amounts=[250_000],
            include_boundary_ages=True,
            include_all_health_classes=False,
            include_tobacco_variants=False,
        )
        ages = {r["applicant"]["age"] for r in records}
        # Should include age band boundaries: 0, 17, 18, 25, 26, 35, etc.
        assert 0 in ages
        assert 17 in ages or 18 in ages

    def test_rate_test_key_format(self):
        from main import ProductType
        records = self.gen.generate(
            product_type=ProductType.TERM_10,
            jurisdictions=["TX"],
            face_amounts=[500_000],
            include_boundary_ages=False,
            include_all_health_classes=False,
            include_tobacco_variants=False,
        )
        for r in records:
            key = r["rate_test_key"]
            assert key.startswith("TX_")
            assert "500000" in key
