"""
Hands Engine — Modular Sub-package Tests.

Tests the enums, constants, and generator modules refactored
from the monolithic hands-engine/main.py.

All tests exercise stub mode (no external services).
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "hands-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Enums ─────────────────────────────────────────────────────


class TestHandsEnums:
    """Test all enums from app.enums."""

    def test_import(self):
        from app.enums import (
            Gender,
            TobaccoStatus,
            HealthClass,
            ProductType,
            RiderType,
            DataStrategy,
        )
        assert Gender is not None
        assert TobaccoStatus is not None
        assert HealthClass is not None

    def test_gender_values(self):
        from app.enums import Gender
        assert len(Gender) == 3

    def test_tobacco_status_values(self):
        from app.enums import TobaccoStatus
        assert len(TobaccoStatus) == 3

    def test_health_class_values(self):
        from app.enums import HealthClass
        assert len(HealthClass) == 7

    def test_product_type_values(self):
        from app.enums import ProductType
        assert len(ProductType) == 12

    def test_rider_type_values(self):
        from app.enums import RiderType
        assert len(RiderType) == 10

    def test_data_strategy_values(self):
        from app.enums import DataStrategy
        assert len(DataStrategy) == 6


# ─── Constants ─────────────────────────────────────────────────


class TestHandsConstants:
    """Test constants from app.constants."""

    def test_import(self):
        from app.constants import (
            US_STATES,
            AGE_BANDS,
            FACE_AMOUNTS,
            PAYMENT_MODES,
            SYNTHETIC_FIRST_NAMES,
            SYNTHETIC_LAST_NAMES,
        )
        assert len(US_STATES) == 51
        assert len(FACE_AMOUNTS) >= 10
        assert len(PAYMENT_MODES) >= 4

    def test_us_states_has_dc(self):
        from app.constants import US_STATES
        assert "DC" in US_STATES

    def test_age_bands_are_dicts(self):
        from app.constants import AGE_BANDS
        assert len(AGE_BANDS) == 8
        assert isinstance(AGE_BANDS[0], dict)
        assert "min" in AGE_BANDS[0]
        assert "max" in AGE_BANDS[0]
        assert "label" in AGE_BANDS[0]

    def test_face_amounts_ordered(self):
        from app.constants import FACE_AMOUNTS
        assert FACE_AMOUNTS == sorted(FACE_AMOUNTS)


# ─── SyntheticProfileGenerator ────────────────────────────────


class TestSyntheticProfileGenerator:
    """Test SyntheticProfileGenerator from app.generators."""

    def test_import(self):
        from app.generators import SyntheticProfileGenerator
        assert SyntheticProfileGenerator is not None

    def test_init(self):
        from app.generators import SyntheticProfileGenerator
        gen = SyntheticProfileGenerator()
        assert gen is not None

    def test_generate_profile(self):
        from app.generators import SyntheticProfileGenerator
        gen = SyntheticProfileGenerator()
        profile = gen.generate_profile()
        assert isinstance(profile, dict)
        # Profile is a nested structure with applicant, policy, etc.
        assert "applicant" in profile or "first_name" in profile
        assert "record_id" in profile

    def test_generate_batch_random(self):
        from app.generators import SyntheticProfileGenerator
        from app.enums import DataStrategy
        gen = SyntheticProfileGenerator()
        batch = gen.generate_batch(count=5, strategy=DataStrategy.RANDOM)
        assert len(batch) == 5
        for p in batch:
            assert "record_id" in p

    def test_generate_batch_boundary(self):
        from app.generators import SyntheticProfileGenerator
        gen = SyntheticProfileGenerator()
        batch = gen.generate_batch(count=3, strategy="boundary")
        assert len(batch) >= 1  # may produce fewer if boundary-only


# ─── CombinatorialGenerator ───────────────────────────────────


class TestCombinatorialGenerator:
    """Test CombinatorialGenerator from app.generators."""

    def test_import(self):
        from app.generators import CombinatorialGenerator
        assert CombinatorialGenerator is not None

    def test_generate_pairwise(self):
        from app.generators import CombinatorialGenerator
        gen = CombinatorialGenerator()
        factors = {"color": ["red", "blue"], "size": ["S", "M", "L"]}
        result = gen.generate_pairwise(factors)
        assert isinstance(result, list)
        assert len(result) >= 3  # at least enough to cover all pairs

    def test_generate_full(self):
        from app.generators import CombinatorialGenerator
        gen = CombinatorialGenerator()
        factors = {"a": [1, 2], "b": [3, 4]}
        result = gen.generate_full(factors)
        assert len(result) == 4  # 2x2 = 4

    def test_pairwise_no_constraints(self):
        from app.generators import CombinatorialGenerator
        factors = {"x": ["a", "b"], "y": ["c", "d"]}
        # generate_pairwise is a static method, no constraints param
        result = CombinatorialGenerator.generate_pairwise(factors)
        assert isinstance(result, list)
        assert len(result) >= 2


# ─── BoundaryValueGenerator ──────────────────────────────────


class TestBoundaryValueGenerator:
    """Test BoundaryValueGenerator from app.generators."""

    def test_import(self):
        from app.generators import BoundaryValueGenerator
        assert BoundaryValueGenerator is not None

    def test_generate_numeric(self):
        from app.generators import BoundaryValueGenerator
        gen = BoundaryValueGenerator()
        spec = {"type": "numeric", "min": 0, "max": 100}
        result = gen.generate(spec)
        assert isinstance(result, list)
        # Should include min, max, min-1, max+1, midpoint etc.
        values = [r.get("value") if isinstance(r, dict) else r for r in result]
        assert len(values) > 0


# ─── PolicyNumberGenerator ───────────────────────────────────


class TestPolicyNumberGenerator:
    """Test PolicyNumberGenerator from app.generators."""

    def test_import(self):
        from app.generators import PolicyNumberGenerator
        assert PolicyNumberGenerator is not None

    def test_generate(self):
        from app.generators import PolicyNumberGenerator
        gen = PolicyNumberGenerator()
        result = gen.generate(
            pattern="{STATE}-{YEAR}-{SEQ:6d}",
            count=3,
        )
        assert isinstance(result, list)
        assert len(result) == 3
        for item in result:
            assert isinstance(item, str)
            assert len(item) > 0


# ─── RateTestDataGenerator ───────────────────────────────────


class TestRateTestDataGenerator:
    """Test RateTestDataGenerator from app.generators."""

    def test_import(self):
        from app.generators import RateTestDataGenerator
        assert RateTestDataGenerator is not None

    def test_generate(self):
        from app.generators import RateTestDataGenerator
        from app.enums import ProductType
        gen = RateTestDataGenerator()
        result = gen.generate(
            product_type=ProductType.WHOLE_LIFE,
            jurisdictions=["CA", "NY"],
            face_amounts=[100000, 250000],
        )
        assert isinstance(result, list)
        assert len(result) >= 1


# ─── Re-exports ───────────────────────────────────────────────


class TestGeneratorsReExports:
    """Verify app.generators.__init__ re-exports all generators."""

    def test_all_generators_exported(self):
        from app.generators import (
            SyntheticProfileGenerator,
            CombinatorialGenerator,
            BoundaryValueGenerator,
            PolicyNumberGenerator,
            RateTestDataGenerator,
        )
        assert all([
            SyntheticProfileGenerator,
            CombinatorialGenerator,
            BoundaryValueGenerator,
            PolicyNumberGenerator,
            RateTestDataGenerator,
        ])


# ─── Integration: main.py v0.2.0 ─────────────────────────────


class TestHandsMainImports:
    """Verify main.py v0.2.0 correctly imports from sub-packages."""

    def test_main_version(self):
        from main import HandsEngine
        engine = HandsEngine()
        assert engine.version == "0.2.0"

    def test_main_config(self):
        from main import HandsConfig
        cfg = HandsConfig()
        assert cfg.engine_name == "hands"
        assert cfg.engine_port == 8008

    def test_main_imports_enums(self):
        from main import Gender, TobaccoStatus, HealthClass
        assert Gender is not None
        assert TobaccoStatus is not None

    def test_main_imports_generators(self):
        from main import (
            SyntheticProfileGenerator,
            CombinatorialGenerator,
            BoundaryValueGenerator,
            PolicyNumberGenerator,
            RateTestDataGenerator,
        )
        assert SyntheticProfileGenerator is not None
        assert RateTestDataGenerator is not None
