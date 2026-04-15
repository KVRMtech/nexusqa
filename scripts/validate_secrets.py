#!/usr/bin/env python3
"""
Nexus QA — Pre-deployment Secrets Validator

Validates that all required secrets are properly configured before deployment.
Runs as a CI gate or local pre-deploy check.

Usage:
    python scripts/validate_secrets.py --env production
    python scripts/validate_secrets.py --env staging --values infrastructure/helm/nexus-qa/values-production.yaml
    python scripts/validate_secrets.py --env docker --env-file infrastructure/docker/.env

Exit codes:
    0 = all secrets valid
    1 = missing or insecure secrets detected
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Secrets that MUST be changed from defaults for production/staging
REQUIRED_SECRETS = {
    "NEXUS_JWT_SECRET": {
        "min_length": 32,
        "description": "JWT signing secret",
        "insecure_patterns": [
            r"nexus-change",
            r"change-me",
            r"^secret$",
            r"^password$",
            r"^test",
            r"^dev",
            r"^example",
            r"^default",
        ],
    },
    "POSTGRES_PASSWORD": {
        "min_length": 12,
        "description": "PostgreSQL database password",
        "insecure_patterns": [
            r"nexus-change",
            r"nexus-db-password",
            r"change-me",
            r"^postgres$",
            r"^password$",
        ],
    },
    "NEO4J_PASSWORD": {
        "min_length": 12,
        "description": "Neo4j graph database password",
        "insecure_patterns": [
            r"nexus-change",
            r"nexus-backbone",
            r"change-me",
            r"^neo4j$",
            r"^password$",
        ],
    },
    "MINIO_SECRET_KEY": {
        "min_length": 16,
        "description": "MinIO object storage secret key",
        "insecure_patterns": [
            r"change-me",
            r"nexus-minio-secret",
            r"^minioadmin$",
        ],
    },
    "GRAFANA_PASSWORD": {
        "min_length": 10,
        "description": "Grafana admin password",
        "insecure_patterns": [
            r"nexus-monitor",
            r"change-me",
            r"^admin$",
            r"^grafana$",
        ],
    },
}

# Secrets that should exist but are less critical
OPTIONAL_SECRETS = {
    "REDIS_PASSWORD": {"min_length": 0, "description": "Redis password (empty = no auth)"},
    "MINIO_ACCESS_KEY": {"min_length": 3, "description": "MinIO access key"},
}


def _is_insecure(value: str, patterns: list[str]) -> str | None:
    """Check if a value matches known insecure patterns."""
    for pattern in patterns:
        if re.search(pattern, value, re.IGNORECASE):
            return pattern
    return None


def validate_env_file(env_file: Path, environment: str) -> list[str]:
    """Validate secrets from a Docker .env file."""
    errors: list[str] = []
    warnings: list[str] = []

    if not env_file.exists():
        errors.append(f"Environment file not found: {env_file}")
        return errors

    # Parse .env file
    secrets: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip()

    # Validate required secrets
    for key, rules in REQUIRED_SECRETS.items():
        value = secrets.get(key, "")
        if not value:
            errors.append(f"  MISSING: {key} — {rules['description']}")
            continue

        if len(value) < rules["min_length"]:
            errors.append(
                f"  TOO SHORT: {key} — must be >= {rules['min_length']} chars, got {len(value)}"
            )

        if environment in ("production", "staging"):
            pattern = _is_insecure(value, rules.get("insecure_patterns", []))
            if pattern:
                errors.append(
                    f"  INSECURE: {key} — matches known default pattern '{pattern}'"
                )

    return errors


def validate_helm_values(values_file: Path, environment: str) -> list[str]:
    """Validate secrets from a Helm values YAML file."""
    errors: list[str] = []

    if not values_file.exists():
        errors.append(f"Values file not found: {values_file}")
        return errors

    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        # Fallback: just check for known insecure defaults via regex
        content = values_file.read_text()
        insecure_defaults = [
            "nexus-change-in-production",
            "nexus-minio-secret-change-me",
            "nexus-minio-dev",
            "nexus-monitor",
        ]
        for default in insecure_defaults:
            if default in content:
                errors.append(f"  INSECURE DEFAULT found: '{default}' in {values_file.name}")
        return errors

    with open(values_file) as f:
        values = yaml.safe_load(f) or {}

    secrets_section = values.get("secrets", {})
    external = values.get("externalSecrets", {})

    # If external secrets are enabled, static validation is N/A
    if external.get("enabled", False):
        print(f"  INFO: External Secrets Operator is enabled — static validation skipped")
        if not external.get("secretStoreRef", {}).get("name"):
            errors.append("  MISCONFIGURED: externalSecrets.enabled=true but secretStoreRef.name is empty")
        return errors

    # Validate static secrets
    key_mapping = {
        "jwtSecret": "NEXUS_JWT_SECRET",
        "postgresPassword": "POSTGRES_PASSWORD",
        "neo4jPassword": "NEO4J_PASSWORD",
        "minioSecretKey": "MINIO_SECRET_KEY",
        "grafanaPassword": "GRAFANA_PASSWORD",
    }

    for yaml_key, env_key in key_mapping.items():
        value = secrets_section.get(yaml_key, "")
        rules = REQUIRED_SECRETS.get(env_key, {})

        if not value:
            errors.append(f"  MISSING: secrets.{yaml_key} — {rules.get('description', '')}")
            continue

        if len(str(value)) < rules.get("min_length", 0):
            errors.append(
                f"  TOO SHORT: secrets.{yaml_key} — must be >= {rules['min_length']} chars"
            )

        if environment in ("production", "staging"):
            pattern = _is_insecure(str(value), rules.get("insecure_patterns", []))
            if pattern:
                errors.append(
                    f"  INSECURE: secrets.{yaml_key} — matches default '{pattern}'"
                )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Nexus QA deployment secrets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["development", "staging", "production", "docker"],
        help="Target environment",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Path to Docker .env file (for docker mode)",
    )
    parser.add_argument(
        "--values",
        type=Path,
        help="Path to Helm values YAML (for staging/production mode)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print(f"  NEXUS QA — SECRETS VALIDATION ({args.env.upper()})")
    print("=" * 60)

    errors: list[str] = []

    if args.env == "docker":
        env_file = args.env_file or Path("infrastructure/docker/.env")
        print(f"\n  Checking: {env_file}")
        errors = validate_env_file(env_file, args.env)

    elif args.env in ("staging", "production"):
        if args.values:
            values_file = args.values
        else:
            values_file = Path(f"infrastructure/helm/nexus-qa/values-{args.env}.yaml")
        print(f"\n  Checking: {values_file}")
        errors = validate_helm_values(values_file, args.env)

    elif args.env == "development":
        print("\n  INFO: Development mode — secrets validation relaxed")
        return 0

    print()

    if errors:
        print("  ERRORS FOUND:")
        for err in errors:
            print(f"    ✗ {err}")
        print()
        print(f"  RESULT: FAILED — {len(errors)} issue(s) detected")
        print()
        print("  To fix:")
        print("    • For Docker: update infrastructure/docker/.env with strong passwords")
        print("    • For K8s:    use --set secrets.X=<value> or enable externalSecrets")
        print("    • Generate:   openssl rand -base64 48")
        print("=" * 60)
        return 1
    else:
        print("  RESULT: PASSED — all secrets validated")
        print("=" * 60)
        return 0


if __name__ == "__main__":
    sys.exit(main())
