"""
Hands Engine — Boundary Value Generator.

Generates boundary value test data for insurance fields: numeric ranges,
dates, and string lengths with at-boundary, just-above, just-below, and
out-of-range test values.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Any


class BoundaryValueGenerator:
    """Generates boundary value test data for insurance fields."""

    @staticmethod
    def generate(
        field_name: str,
        field_type: str = "numeric",
        min_value: Optional[Any] = None,
        max_value: Optional[Any] = None,
        boundary_points: Optional[list] = None,
        include_invalid: bool = True,
    ) -> list[dict]:
        """Generate boundary values for a field."""
        results: list[dict] = []

        if field_type == "numeric":
            results = BoundaryValueGenerator._numeric_boundaries(
                field_name, min_value, max_value, boundary_points or [], include_invalid
            )
        elif field_type == "date":
            results = BoundaryValueGenerator._date_boundaries(
                field_name, min_value, max_value, boundary_points or [], include_invalid
            )
        elif field_type == "string":
            results = BoundaryValueGenerator._string_boundaries(
                field_name, min_value, max_value, include_invalid
            )

        return results

    @staticmethod
    def _numeric_boundaries(
        field_name: str,
        min_val: Optional[float],
        max_val: Optional[float],
        points: list,
        include_invalid: bool,
    ) -> list[dict]:
        bvs: list[dict] = []
        all_points: list[tuple[str, float]] = []

        if min_val is not None:
            all_points.append(("min", float(min_val)))
        if max_val is not None:
            all_points.append(("max", float(max_val)))
        for p in points:
            all_points.append(("boundary", float(p)))

        for label, val in all_points:
            # At boundary
            bvs.append({
                "field": field_name,
                "value": val,
                "category": f"at_{label}",
                "expected_valid": True,
                "description": f"{field_name} exactly at {label} ({val})",
            })
            # Just above
            bvs.append({
                "field": field_name,
                "value": val + 1,
                "category": f"above_{label}",
                "expected_valid": True if label != "max" else False,
                "description": f"{field_name} just above {label} ({val + 1})",
            })
            # Just below
            bvs.append({
                "field": field_name,
                "value": val - 1,
                "category": f"below_{label}",
                "expected_valid": True if label != "min" else False,
                "description": f"{field_name} just below {label} ({val - 1})",
            })

        if include_invalid:
            if min_val is not None:
                bvs.append({
                    "field": field_name,
                    "value": float(min_val) - 100,
                    "category": "far_below_min",
                    "expected_valid": False,
                    "description": f"{field_name} far below minimum",
                })
            if max_val is not None:
                bvs.append({
                    "field": field_name,
                    "value": float(max_val) + 100,
                    "category": "far_above_max",
                    "expected_valid": False,
                    "description": f"{field_name} far above maximum",
                })
            # Zero and negative
            bvs.append({
                "field": field_name, "value": 0,
                "category": "zero", "expected_valid": min_val is not None and float(min_val) <= 0,
                "description": f"{field_name} = 0",
            })
            bvs.append({
                "field": field_name, "value": -1,
                "category": "negative", "expected_valid": False,
                "description": f"{field_name} = -1 (negative)",
            })

        return bvs

    @staticmethod
    def _date_boundaries(
        field_name: str,
        min_val: Optional[str],
        max_val: Optional[str],
        points: list,
        include_invalid: bool,
    ) -> list[dict]:
        bvs: list[dict] = []
        today = date.today()

        dates_to_test: list[tuple[str, date]] = []
        if min_val:
            d = date.fromisoformat(min_val) if isinstance(min_val, str) else min_val
            dates_to_test.append(("min", d))
        if max_val:
            d = date.fromisoformat(max_val) if isinstance(max_val, str) else max_val
            dates_to_test.append(("max", d))

        for label, d in dates_to_test:
            bvs.append({"field": field_name, "value": d.isoformat(), "category": f"at_{label}", "expected_valid": True})
            bvs.append({"field": field_name, "value": (d + timedelta(days=1)).isoformat(), "category": f"day_after_{label}", "expected_valid": label != "max"})
            bvs.append({"field": field_name, "value": (d - timedelta(days=1)).isoformat(), "category": f"day_before_{label}", "expected_valid": label != "min"})

        if include_invalid:
            bvs.append({"field": field_name, "value": "9999-12-31", "category": "far_future", "expected_valid": False})
            bvs.append({"field": field_name, "value": "1900-01-01", "category": "far_past", "expected_valid": False})
            bvs.append({"field": field_name, "value": today.isoformat(), "category": "today", "expected_valid": True})

        return bvs

    @staticmethod
    def _string_boundaries(
        field_name: str,
        min_length: Optional[int],
        max_length: Optional[int],
        include_invalid: bool,
    ) -> list[dict]:
        bvs: list[dict] = []
        min_len = int(min_length) if min_length else 0
        max_len = int(max_length) if max_length else 255

        bvs.append({"field": field_name, "value": "A" * min_len, "category": "min_length", "expected_valid": True})
        bvs.append({"field": field_name, "value": "A" * max_len, "category": "max_length", "expected_valid": True})
        if min_len > 0:
            bvs.append({"field": field_name, "value": "A" * (min_len - 1), "category": "below_min_length", "expected_valid": False})
        bvs.append({"field": field_name, "value": "A" * (max_len + 1), "category": "above_max_length", "expected_valid": False})

        if include_invalid:
            bvs.append({"field": field_name, "value": "", "category": "empty_string", "expected_valid": min_len == 0})
            bvs.append({"field": field_name, "value": " " * max_len, "category": "all_spaces", "expected_valid": False})
            bvs.append({"field": field_name, "value": "A" * max_len + "!@#$%", "category": "special_chars_overflow", "expected_valid": False})

        return bvs
