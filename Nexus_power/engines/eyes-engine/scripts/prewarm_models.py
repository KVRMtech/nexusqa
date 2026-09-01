from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _languages() -> list[str]:
    raw = os.environ.get("EYES_OCR_LANGUAGES", "en")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _model_dir() -> str:
    return os.environ.get("EYES_OCR_MODEL_DIR", "./models/easyocr")


def _required_files(languages: list[str]) -> set[str]:
    required = {"craft_mlt_25k.pth"}
    if "en" in languages:
        required.add("english_g2.pth")
    return required


def _has_model_artifacts(model_dir: str, languages: list[str]) -> bool:
    path = Path(model_dir)
    if not path.exists() or not path.is_dir():
        return False
    present = {child.name for child in path.iterdir() if child.is_file()}
    required = _required_files(languages)
    return required.issubset(present)


def _verify() -> int:
    languages = _languages()
    model_dir = _model_dir()
    if not _has_model_artifacts(model_dir, languages):
        print(
            f"missing EasyOCR artifacts for languages={languages} in {model_dir}",
            file=sys.stderr,
        )
        return 1
    print(f"verified EasyOCR artifacts for languages={languages} in {model_dir}")
    return 0


def _prewarm() -> int:
    import easyocr  # type: ignore[import-not-found]

    languages = _languages()
    model_dir = _model_dir()
    if _has_model_artifacts(model_dir, languages):
        print(f"EasyOCR artifacts already present in {model_dir}")
        return 0

    print(f"prewarming EasyOCR models for languages={languages} into {model_dir}")
    easyocr.Reader(
        languages,
        gpu=False,
        model_storage_directory=model_dir,
    )
    return _verify()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["prewarm", "verify"], default="prewarm")
    args = parser.parse_args()

    if args.mode == "verify":
        return _verify()
    return _prewarm()


if __name__ == "__main__":
    raise SystemExit(main())