#!/usr/bin/env python3
"""
Nexus QA — Ollama Model Setup Script.

Pulls required models for all AI engines:
    - Heart/Brain local tier: llama3.2:1b (CPU-safe text model)
    - Eyes Engine:             llama3.2-vision:11b  (Meta Llama vision model)

Usage:
    python scripts/setup_ollama.py

Prerequisites:
    - Ollama must be running (ollama serve or Docker)
    - Default: http://localhost:11434
"""

import asyncio
import sys
import os
import httpx

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

REQUIRED_MODELS = [
    {
        "name": "llama3.2:1b",
        "engine": "Heart + Brain local tier",
        "purpose": "CPU-safe text reasoning fallback for local/dev deployments",
        "size": "~1.3 GB",
    },
    {
        "name": "llava:7b",
        "engine": "Eyes (Vision) — fast profile",
        "purpose": "Lightweight vision model for fast-profile frame analysis",
        "size": "~4.7 GB",
    },
    {
        "name": "llama3.2-vision:11b",
        "engine": "Eyes (Vision) — deep profile",
        "purpose": "Screenshot and video frame analysis for visual workflow understanding (Meta Llama)",
        "size": "~7.9 GB",
    },
]


async def check_ollama():
    """Check if Ollama is running."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{OLLAMA_BASE}/api/tags")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                return True, [m.get("name", "") for m in models]
            return False, []
        except Exception as e:
            return False, []


async def pull_model(model_name: str):
    """Pull a model from Ollama registry."""
    print(f"  Pulling {model_name} (this may take several minutes)...")
    async with httpx.AsyncClient(timeout=1800.0) as client:
        resp = await client.post(
            f"{OLLAMA_BASE}/api/pull",
            json={"name": model_name, "stream": False},
        )
        if resp.status_code == 200:
            print(f"  ✓ {model_name} pulled successfully")
            return True
        else:
            print(f"  ✗ Failed to pull {model_name}: HTTP {resp.status_code}")
            return False


async def main():
    print("=" * 60)
    print("Nexus QA — Ollama Model Setup")
    print("=" * 60)
    print(f"\nOllama endpoint: {OLLAMA_BASE}")

    running, existing_models = await check_ollama()
    if not running:
        print("\n✗ Ollama is NOT running!")
        print("  Start Ollama with: ollama serve")
        print("  Or with Docker:    docker compose -f docker-compose.dev.yml up ollama -d")
        sys.exit(1)

    print(f"✓ Ollama is running ({len(existing_models)} models installed)")

    if existing_models:
        print(f"  Installed: {', '.join(existing_models)}")

    print(f"\n--- Required Models ({len(REQUIRED_MODELS)}) ---\n")

    all_ok = True
    for model_info in REQUIRED_MODELS:
        name = model_info["name"]
        already = any(name in m for m in existing_models)
        status = "✓ installed" if already else "○ needs pull"
        print(f"  [{status}] {name}")
        print(f"    Engine:  {model_info['engine']}")
        print(f"    Purpose: {model_info['purpose']}")
        print(f"    Size:    {model_info['size']}")
        print()

        if not already:
            success = await pull_model(name)
            if not success:
                all_ok = False

    print("\n" + "=" * 60)
    if all_ok:
        print("✓ All models ready! Engines can now use real AI inference.")
        print("\nVerify with: ollama list")
    else:
        print("⚠ Some models failed to pull. Check your network and retry.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
