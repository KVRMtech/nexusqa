"""
Nexus Hands Engine â€” Synthetic Test Data Generation (v0.2.0 Modular).

The hands that prepare data before tests run. Insurance testing requires
a combinatorial explosion of test data:

- Age bands (0-17, 18-25, 26-35, 36-45, 46-55, 56-65, 66-75, 76+)
- Gender (Male, Female, Non-binary)
- Tobacco status (Smoker, Non-smoker, Former smoker)
- State/Jurisdiction (50 states + territories, each with unique regs)
- Product types (Term, Whole, UL, VUL, IUL, Annuity, etc.)
- Riders (WOP, ADB, GPO, CIR, COLA, etc.)
- Health classes (Preferred Plus, Preferred, Standard Plus, Standard, Substandard)
- Beneficiary types (Individual, Trust, Estate, Charity)

v0.2.0 â€” Refactored: enums, constants, and generators extracted to app/
sub-package for maintainability and testability.
"""

from __future__ import annotations

import time
from typing import Optional, Any

from fastapi import Depends, HTTPException, BackgroundTasks
from pydantic import Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.models import NexusRequest, NexusResponse
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent

# â”€â”€â”€ Imports from modular app/ sub-package â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

from app.enums import (
    Gender, TobaccoStatus, HealthClass, ProductType, RiderType, DataStrategy,
)
from app.constants import US_STATES, AGE_BANDS, FACE_AMOUNTS, PAYMENT_MODES
from app.generators import (
    SyntheticProfileGenerator,
    CombinatorialGenerator,
    BoundaryValueGenerator,
    PolicyNumberGenerator,
    RateTestDataGenerator,
)


# â”€â”€â”€ Configuration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class HandsConfig(EngineConfig):
    engine_name: str = "hands"
    engine_port: int = 8008

    # Generation limits
    max_records_per_request: int = 10_000
    max_combinations: int = 50_000
    default_batch_size: int = 500

    # Synthetic data seed (for reproducibility)
    random_seed: Optional[int] = None

    # Insurance-specific
    default_jurisdiction: str = "ALL"


# â”€â”€â”€ Request / Response Models â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class GenerateProfilesRequest(NexusRequest):
    """Request to generate synthetic applicant profiles."""
    count: int = Field(default=10, ge=1, le=10_000, description="Number of profiles to generate")
    product_type: Optional[ProductType] = Field(default=None, description="Target product type")
    jurisdictions: list[str] = Field(
        default_factory=list,
        description="State codes to include (empty = all states)",
    )
    age_range: Optional[dict] = Field(
        default=None, description="Override age range: {'min': 18, 'max': 65}",
    )
    include_riders: bool = Field(default=True, description="Include rider selections")
    include_beneficiaries: bool = Field(default=True, description="Include beneficiary info")
    strategy: DataStrategy = Field(
        default=DataStrategy.RANDOM, description="Data generation strategy",
    )


class GenerateProfilesResponse(NexusResponse):
    profiles: list[dict] = Field(default_factory=list)
    generation_stats: dict = Field(default_factory=dict)


class GenerateCombinatorialRequest(NexusRequest):
    """Request to generate combinatorial test data sets."""
    dimensions: dict[str, list[Any]] = Field(
        ...,
        description="Dimensions to combine: {'age': [25, 45, 65], 'gender': ['M', 'F'], ...}",
    )
    strategy: DataStrategy = Field(default=DataStrategy.PAIRWISE)
    max_combinations: int = Field(default=1000, le=50_000)
    constraints: list[dict] = Field(
        default_factory=list,
        description="Rules to filter combos: [{'if': {'age': '<18'}, 'then': {'product': '!= whole_life'}}]",
    )


class GenerateCombinatorialResponse(NexusResponse):
    combinations: list[dict] = Field(default_factory=list)
    total_possible: int = 0
    total_generated: int = 0
    coverage_percent: float = 0.0


class GenerateBoundaryRequest(NexusRequest):
    """Request to generate boundary value test data."""
    field_name: str = Field(..., description="Field to generate boundaries for")
    field_type: str = Field(default="numeric", description="numeric, date, string")
    min_value: Optional[Any] = None
    max_value: Optional[Any] = None
    boundary_points: list[Any] = Field(
        default_factory=list,
        description="Known boundary points (e.g., rate table breaks at age 30, 40, 50)",
    )
    include_invalid: bool = Field(
        default=True, description="Include just-outside-boundary invalid values",
    )


class GenerateBoundaryResponse(NexusResponse):
    boundary_values: list[dict] = Field(default_factory=list)
    total_generated: int = 0


class GeneratePolicyNumbersRequest(NexusRequest):
    """Generate synthetic policy/claim numbers matching carrier formats."""
    format_pattern: str = Field(
        default="POL-{STATE}-{YEAR}-{SEQ:06d}",
        description="Pattern: {STATE}, {YEAR}, {SEQ:Nd}, {ALPHA:N}, {DIGIT:N}",
    )
    count: int = Field(default=10, ge=1, le=10_000)
    start_sequence: int = Field(default=1)


class GeneratePolicyNumbersResponse(NexusResponse):
    numbers: list[str] = Field(default_factory=list)


class GenerateRateTestDataRequest(NexusRequest):
    """Generate test data for rate table validation."""
    product_type: ProductType
    jurisdictions: list[str] = Field(default_factory=lambda: ["NY", "CA", "TX", "FL", "IL"])
    include_boundary_ages: bool = Field(default=True)
    include_all_health_classes: bool = Field(default=True)
    include_tobacco_variants: bool = Field(default=True)
    face_amounts: list[int] = Field(default_factory=lambda: [25_000, 100_000, 500_000, 1_000_000])


class GenerateRateTestDataResponse(NexusResponse):
    test_records: list[dict] = Field(default_factory=list)
    total_generated: int = 0
    dimensions_covered: dict = Field(default_factory=dict)


# â”€â”€â”€ The Hands Engine â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class HandsEngine(NexusEngine):

    def __init__(self):
        super().__init__(
            name="hands",
            version="0.2.0",
            config=HandsConfig(engine_name="hands", engine_port=8008),
            description="Synthetic test data generation for insurance QA",
        )
        self.profile_generator: Optional[SyntheticProfileGenerator] = None
        self.rate_generator: Optional[RateTestDataGenerator] = None
        self.jobs: dict = {}

    async def on_startup(self):
        """Initialize generators and load data generator extensions from plugins."""
        seed = self.config.random_seed if hasattr(self.config, "random_seed") else None
        self.profile_generator = SyntheticProfileGenerator(seed=seed)
        self.rate_generator = RateTestDataGenerator(seed=seed)
        self.health.set_mode("data_generation", "synthetic")

        # Load data generator extensions from domain plugins
        try:
            data_gen_ext = self.plugin_registry.get_merged_data_generators()
            if data_gen_ext:
                self._plugin_data_profiles = data_gen_ext.data_profiles or []
                self._plugin_id_patterns = data_gen_ext.id_patterns or []
        except Exception:
            self._plugin_data_profiles = []
            self._plugin_id_patterns = []

        # Subscribe to events: when Heart generates test cases, auto-generate test data
        if self.event_bus:
            await self.event_bus.subscribe(
                "heart.tests.generated",
                self._on_tests_generated,
            )

    async def _on_tests_generated(self, event: NexusEvent):
        """Auto-generate test data when Heart creates test cases."""
        data = event.data or {}
        test_cases = data.get("test_cases", [])
        tenant_id = data.get("tenant_id", "")

        if not test_cases:
            return

        profiles = self.profile_generator.generate_batch(
            count=len(test_cases) * 3,
            strategy=DataStrategy.BOUNDARY,
        )

        if self.event_bus:
            await self.event_bus.publish(NexusEvent(
                event_type="hands.data.generated",
                source="hands",
                data={
                    "tenant_id": tenant_id,
                    "profile_count": len(profiles),
                    "triggered_by": "heart.tests.generated",
                },
            ))

    def register_routes(self, app):

        engine = self

        # â”€â”€ Generate Synthetic Profiles â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        @app.post(
            "/api/v1/hands/generate-profiles",
            response_model=GenerateProfilesResponse,
        )
        async def generate_profiles(
            req: GenerateProfilesRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate synthetic insurance applicant profiles."""
            start = time.monotonic()

            if req.count > engine.config.max_records_per_request:
                raise HTTPException(
                    status_code=400,
                    detail=f"Max {engine.config.max_records_per_request} records per request",
                )

            profiles = engine.profile_generator.generate_batch(
                count=req.count,
                product_type=req.product_type,
                jurisdictions=req.jurisdictions or None,
                age_range=req.age_range,
                strategy=req.strategy,
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            age_distribution: dict[str, int] = {}
            state_distribution: dict[str, int] = {}
            tobacco_distribution: dict[str, int] = {}
            for p in profiles:
                band = p.get("test_metadata", {}).get("age_band", "unknown")
                age_distribution[band] = age_distribution.get(band, 0) + 1
                state = p.get("policy", {}).get("jurisdiction", "unknown")
                state_distribution[state] = state_distribution.get(state, 0) + 1
                tobacco = p.get("applicant", {}).get("tobacco_status", "unknown")
                tobacco_distribution[tobacco] = tobacco_distribution.get(tobacco, 0) + 1

            return GenerateProfilesResponse(
                success=True,
                trace_id=req.trace_id,
                engine="hands",
                engine_version="0.2.0",
                processing_time_ms=elapsed_ms,
                profiles=profiles,
                generation_stats={
                    "total_generated": len(profiles),
                    "strategy": req.strategy.value,
                    "age_distribution": age_distribution,
                    "state_distribution": state_distribution,
                    "tobacco_distribution": tobacco_distribution,
                },
            )

        # â”€â”€ Generate Combinatorial Data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        @app.post(
            "/api/v1/hands/generate-combinatorial",
            response_model=GenerateCombinatorialResponse,
        )
        async def generate_combinatorial(
            req: GenerateCombinatorialRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate combinatorial test data (pairwise or full Cartesian)."""
            start = time.monotonic()

            total_possible = 1
            for vals in req.dimensions.values():
                total_possible *= len(vals)

            if req.strategy == DataStrategy.PAIRWISE:
                combos = CombinatorialGenerator.generate_pairwise(
                    req.dimensions, req.max_combinations,
                )
            elif req.strategy == DataStrategy.FULL_COMBINATORIAL:
                combos = CombinatorialGenerator.generate_full(
                    req.dimensions, req.max_combinations, req.constraints,
                )
            else:
                combos = CombinatorialGenerator.generate_pairwise(
                    req.dimensions, req.max_combinations,
                )

            elapsed_ms = (time.monotonic() - start) * 1000
            coverage = (len(combos) / max(total_possible, 1)) * 100

            return GenerateCombinatorialResponse(
                success=True,
                trace_id=req.trace_id,
                engine="hands",
                engine_version="0.2.0",
                processing_time_ms=elapsed_ms,
                combinations=combos,
                total_possible=total_possible,
                total_generated=len(combos),
                coverage_percent=round(min(coverage, 100.0), 2),
            )

        # â”€â”€ Generate Boundary Values â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        @app.post(
            "/api/v1/hands/generate-boundary",
            response_model=GenerateBoundaryResponse,
        )
        async def generate_boundary(
            req: GenerateBoundaryRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate boundary value test data for a specific field."""
            start = time.monotonic()

            bvs = BoundaryValueGenerator.generate(
                field_name=req.field_name,
                field_type=req.field_type,
                min_value=req.min_value,
                max_value=req.max_value,
                boundary_points=req.boundary_points,
                include_invalid=req.include_invalid,
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            return GenerateBoundaryResponse(
                success=True,
                trace_id=req.trace_id,
                engine="hands",
                engine_version="0.2.0",
                processing_time_ms=elapsed_ms,
                boundary_values=bvs,
                total_generated=len(bvs),
            )

        # â”€â”€ Generate Policy Numbers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        @app.post(
            "/api/v1/hands/generate-policy-numbers",
            response_model=GeneratePolicyNumbersResponse,
        )
        async def generate_policy_numbers(
            req: GeneratePolicyNumbersRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate synthetic policy/claim/agent numbers matching carrier formats."""
            numbers = PolicyNumberGenerator.generate(
                pattern=req.format_pattern,
                count=req.count,
                start_sequence=req.start_sequence,
            )

            return GeneratePolicyNumbersResponse(
                success=True,
                trace_id=req.trace_id,
                engine="hands",
                engine_version="0.2.0",
                numbers=numbers,
            )

        # â”€â”€ Generate Rate Table Test Data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        @app.post(
            "/api/v1/hands/generate-rate-data",
            response_model=GenerateRateTestDataResponse,
        )
        async def generate_rate_data(
            req: GenerateRateTestDataRequest,
            background_tasks: BackgroundTasks,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate comprehensive test data for rate table validation."""
            start = time.monotonic()

            records = engine.rate_generator.generate(
                product_type=req.product_type,
                jurisdictions=req.jurisdictions,
                face_amounts=req.face_amounts,
                include_boundary_ages=req.include_boundary_ages,
                include_all_health_classes=req.include_all_health_classes,
                include_tobacco_variants=req.include_tobacco_variants,
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            dimensions_covered = {
                "jurisdictions": len(req.jurisdictions),
                "ages": len(set(r["applicant"]["age"] for r in records)),
                "genders": len(set(r["applicant"]["gender"] for r in records)),
                "tobacco_statuses": len(set(r["applicant"]["tobacco_status"] for r in records)),
                "health_classes": len(set(r["applicant"]["health_class"] for r in records)),
                "face_amounts": len(req.face_amounts),
            }

            if engine.event_bus:
                try:
                    await engine.event_bus.publish(NexusEvent(
                        event_type="hands.rate_data.generated",
                        source="hands",
                        data={
                            "tenant_id": req.tenant_id,
                            "product_type": req.product_type.value,
                            "record_count": len(records),
                        },
                    ))
                except Exception:
                    pass

            return GenerateRateTestDataResponse(
                success=True,
                trace_id=req.trace_id,
                engine="hands",
                engine_version="0.2.0",
                processing_time_ms=elapsed_ms,
                test_records=records,
                total_generated=len(records),
                dimensions_covered=dimensions_covered,
            )

        # â”€â”€ Stats â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        @app.get("/api/v1/hands/stats")
        async def get_stats(user: NexusUser = Depends(get_current_user)):
            """Engine statistics."""
            return {
                "engine": "hands",
                "version": "0.2.0",
                "capabilities": [
                    "synthetic_profiles",
                    "combinatorial_generation",
                    "boundary_value_analysis",
                    "policy_number_generation",
                    "rate_table_test_data",
                    "pairwise_coverage",
                ],
                "supported_products": [p.value for p in ProductType],
                "supported_strategies": [s.value for s in DataStrategy],
                "jurisdictions_available": len(US_STATES),
                "age_bands": len(AGE_BANDS),
            }


# â”€â”€â”€ Entry Point â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if __name__ == "__main__":
    HandsEngine().run()
