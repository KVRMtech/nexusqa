from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from app.diarization.bundle import (
    materialize_bundle_from_uri,
    prepare_bundle_from_huggingface,
    verify_bundle,
)

REQUIRED_FILES = {"model.bin", "config.json", "tokenizer.json"}
PYANNOTE_REQUIRED_FILES = {"config.yaml", "config.yml"}


def _model_specs() -> list[tuple[str, str, str]]:
    specs = [
        (
            "fast",
            os.environ.get("WHISPER_FAST_MODEL_SIZE", "medium"),
            os.environ.get("WHISPER_FAST_MODEL_PATH", "./models/whisper-medium"),
        ),
        (
            "deep",
            os.environ.get("WHISPER_MODEL_SIZE", "large-v3"),
            os.environ.get("WHISPER_MODEL_PATH", "./models/whisper-large-v3"),
        ),
    ]
    deduped: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for profile, model_size, model_path in specs:
        key = (model_size, model_path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((profile, model_size, model_path))
    return deduped


def _has_model_artifacts(model_path: str) -> bool:
    path = Path(model_path)
    if path.exists() and path.is_dir():
        present = {child.name for child in path.iterdir() if child.is_file()}
        if any(name in present for name in REQUIRED_FILES):
            return True

    hub_cache_dir = path.parent / f"models--Systran--faster-whisper-{path.name.replace('whisper-', '')}"
    snapshots_dir = hub_cache_dir / "snapshots"
    if not snapshots_dir.exists() or not snapshots_dir.is_dir():
        return False

    for snapshot_dir in snapshots_dir.iterdir():
        if not snapshot_dir.is_dir():
            continue
        snapshot_files = {child.name for child in snapshot_dir.iterdir() if child.is_file()}
        if any(name in snapshot_files for name in REQUIRED_FILES):
            return True
    return False


def _verify() -> int:
    missing: list[str] = []
    for profile, model_size, model_path in _model_specs():
        if _has_model_artifacts(model_path):
            print(f"verified whisper {profile} model {model_size} at {model_path}")
        else:
            missing.append(f"{profile}:{model_size}:{model_path}")
    diarizer_enabled = _bool_env("EARS_PREWARM_DIARIZER", default=False)
    if diarizer_enabled:
        diarizer_path = os.environ.get("PYANNOTE_MODEL_PATH", "./models/pyannote-speaker-3.1")
        require_manifest = _bool_env("PYANNOTE_REQUIRE_MANIFEST", default=False)
        bundle_ok, bundle_reason = verify_bundle(diarizer_path)
        if bundle_ok:
            print(f"verified pyannote diarizer bundle at {diarizer_path}")
        elif _has_pyannote_artifacts(diarizer_path) and not require_manifest:
            print(f"verified pyannote diarizer artifacts at {diarizer_path}")
        else:
            missing.append(f"diarizer:{diarizer_path}:{bundle_reason}")
    if missing:
        print("missing model artifacts: " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


def _prewarm() -> int:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    compute_type = os.environ.get("WHISPER_PREWARM_COMPUTE_TYPE", "int8")
    for profile, model_size, model_path in _model_specs():
        if _has_model_artifacts(model_path):
            print(f"whisper {profile} model already present: {model_size} @ {model_path}")
            continue

        download_root = str(Path(model_path).parent)
        print(f"prewarming whisper {profile} model {model_size} into {download_root}")
        WhisperModel(
            model_size,
            device="cpu",
            compute_type=compute_type,
            download_root=download_root,
        )
        if not _has_model_artifacts(model_path):
            print(
                f"whisper prewarm incomplete for {profile} model {model_size} at {model_path}",
                file=sys.stderr,
            )
            return 1

    if _bool_env("EARS_PREWARM_DIARIZER", default=False):
        diarizer_path = os.environ.get("PYANNOTE_MODEL_PATH", "./models/pyannote-speaker-3.1")
        diarizer_repo_id = os.environ.get("PYANNOTE_REPO_ID", "pyannote/speaker-diarization-3.1")
        bundle_uri = os.environ.get("PYANNOTE_BUNDLE_URI", "").strip()
        bundle_sha256 = os.environ.get("PYANNOTE_BUNDLE_SHA256", "").strip()
        hf_token = os.environ.get("HF_TOKEN", "")
        bundle_ok, bundle_reason = verify_bundle(diarizer_path)
        if bundle_ok:
            print(f"pyannote diarizer bundle already present at {diarizer_path}")
        elif bundle_uri:
            print(f"materializing pyannote diarizer bundle from {bundle_uri} into {diarizer_path}")
            ok, reason = materialize_bundle_from_uri(
                bundle_uri,
                diarizer_path,
                expected_sha256=bundle_sha256,
            )
            if not ok:
                print(f"pyannote bundle materialization failed: {reason}", file=sys.stderr)
                return 1
        elif hf_token:
            print(f"building pyannote diarizer bundle {diarizer_repo_id} into {diarizer_path}")
            from huggingface_hub.errors import GatedRepoError  # type: ignore[import-not-found]

            try:
                ok, reason = prepare_bundle_from_huggingface(
                    diarizer_path,
                    repo_id=diarizer_repo_id,
                    hf_token=hf_token,
                )
            except GatedRepoError:
                print(
                    (
                        f"pyannote prewarm blocked: token is not authorized for gated repo {diarizer_repo_id}. "
                        "Request access on Hugging Face and retry the seed job."
                    ),
                    file=sys.stderr,
                )
                return 1
            if not ok:
                print(f"pyannote bundle preparation failed: {reason}", file=sys.stderr)
                return 1
        else:
            print(
                "pyannote prewarm requested but neither PYANNOTE_BUNDLE_URI nor HF_TOKEN is configured",
                file=sys.stderr,
            )
            return 1

    return _verify()


def _bool_env(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _has_pyannote_artifacts(model_path: str) -> bool:
    path = Path(model_path)
    if not path.exists() or not path.is_dir():
        return False
    present = {child.name for child in path.iterdir() if child.is_file()}
    return any(name in present for name in PYANNOTE_REQUIRED_FILES)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["prewarm", "verify"], default="prewarm")
    args = parser.parse_args()

    if args.mode == "verify":
        return _verify()
    return _prewarm()


if __name__ == "__main__":
    raise SystemExit(main())