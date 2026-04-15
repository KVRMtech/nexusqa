from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.diarization.bundle import create_bundle_archive, prepare_bundle_from_huggingface


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acquire a self-contained Pyannote diarization bundle for internal mirroring.",
    )
    parser.add_argument(
        "--output-dir",
        default="./models/pyannote-speaker-3.1",
        help="Directory to write the self-contained diarization bundle into.",
    )
    parser.add_argument(
        "--repo-id",
        default=os.environ.get("PYANNOTE_REPO_ID", "pyannote/speaker-diarization-3.1"),
        help="Top-level diarization repo ID.",
    )
    parser.add_argument(
        "--archive-path",
        default="",
        help="Optional .tar.gz path to package the resulting bundle for internal mirroring.",
    )
    args = parser.parse_args()

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        print("HF_TOKEN is required for onboarding the diarization bundle", file=sys.stderr)
        return 1

    bundle_dir = Path(args.output_dir)
    bundle_dir.parent.mkdir(parents=True, exist_ok=True)

    ok, reason = prepare_bundle_from_huggingface(
        bundle_dir,
        repo_id=args.repo_id,
        hf_token=hf_token,
    )
    if not ok:
        print(f"bundle onboarding failed: {reason}", file=sys.stderr)
        return 1

    print(f"prepared diarization bundle at {bundle_dir}")

    if args.archive_path:
        archive_path, checksum = create_bundle_archive(bundle_dir, args.archive_path)
        print(f"packaged diarization bundle at {archive_path}")
        print(f"archive sha256: {checksum}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())