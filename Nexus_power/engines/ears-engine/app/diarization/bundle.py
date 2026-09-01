from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_FILENAME = "bundle.manifest.json"
_PIPELINE_REF_PATTERN = re.compile(r"^(?P<indent>\s*)(?P<key>embedding|segmentation|plda):\s*(?P<value>[^\s#]+)\s*$", re.MULTILINE)
_HF_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMPATIBILITY_ASSETS: dict[str, dict[str, tuple[str, ...]]] = {
    "pyannote/speaker-diarization-3.1": {
        "pyannote/speaker-diarization-community-1": ("plda",),
    }
}


def manifest_path(bundle_root: str | Path) -> Path:
    return Path(bundle_root) / MANIFEST_FILENAME


def sanitize_repo_id(repo_id: str) -> str:
    return repo_id.replace("/", "--")


def extract_pipeline_model_refs(config_text: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for match in _PIPELINE_REF_PATTERN.finditer(config_text):
        refs[match.group("key")] = match.group("value")
    return refs


def is_huggingface_repo_ref(value: str) -> bool:
    if value.startswith(("./", "../", "deps/")):
        return False
    return bool(_HF_REPO_PATTERN.fullmatch(value))


def rewrite_pipeline_refs(config_text: str, replacements: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group("key")
        value = match.group("value")
        new_value = replacements.get(value)
        if not new_value:
            return match.group(0)
        return f"{match.group('indent')}{key}: {new_value}"

    return _PIPELINE_REF_PATTERN.sub(replace, config_text)


def prepare_runtime_bundle(bundle_root: str | Path) -> tuple[Path, Path | None]:
    root = Path(bundle_root).resolve()
    config_path = root / "config.yaml"
    if not config_path.exists():
        return root, None

    config_text = config_path.read_text(encoding="utf-8")
    refs = extract_pipeline_model_refs(config_text)
    replacements: dict[str, str] = {}

    for value in refs.values():
        if is_huggingface_repo_ref(value):
            continue
        resolved = (root / value).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.exists():
            replacements[value] = resolved.as_posix()

    if not replacements:
        return root, None

    temp_root = Path(tempfile.mkdtemp(prefix=f"{root.name}-runtime-"))
    runtime_root = temp_root / root.name
    shutil.copytree(root, runtime_root)
    handler_path = runtime_root / "handler.py"
    if handler_path.exists():
        handler_path.unlink()
    (runtime_root / "config.yaml").write_text(
        rewrite_pipeline_refs(config_text, replacements),
        encoding="utf-8",
    )
    return runtime_root, temp_root


def file_sha256(file_path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(file_path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_cache_metadata(bundle_root: str | Path) -> None:
    for cache_dir in Path(bundle_root).rglob(".cache"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)


def copy_compatibility_assets(
    bundle_root: str | Path,
    deps_root: str | Path,
    *,
    repo_id: str,
    hf_token: str,
) -> list[str]:
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    root = Path(bundle_root)
    deps = Path(deps_root)
    mirrored: list[str] = []
    compatibility_repos = _COMPATIBILITY_ASSETS.get(repo_id, {})

    for compatibility_repo, asset_paths in compatibility_repos.items():
        compatibility_dir = deps / sanitize_repo_id(compatibility_repo)
        allow_patterns: list[str] = []
        for asset_path in asset_paths:
            asset = Path(asset_path)
            allow_patterns.append(asset.as_posix())
            allow_patterns.append(f"{asset.as_posix()}/**")
        snapshot_download(
            repo_id=compatibility_repo,
            token=hf_token,
            local_dir=str(compatibility_dir),
            local_dir_use_symlinks=False,
            allow_patterns=allow_patterns,
        )
        mirrored.append(compatibility_repo)
        for asset_path in asset_paths:
            source = compatibility_dir / asset_path
            target = root / asset_path
            if source.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    return mirrored


def build_manifest(bundle_root: str | Path, *, metadata: dict | None = None) -> dict:
    root = Path(bundle_root)
    files: list[dict[str, int | str]] = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.name == MANIFEST_FILENAME:
            continue
        rel_path = file_path.relative_to(root).as_posix()
        files.append(
            {
                "path": rel_path,
                "sha256": file_sha256(file_path),
                "size": file_path.stat().st_size,
            }
        )

    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "metadata": metadata or {},
    }


def write_manifest(bundle_root: str | Path, manifest: dict) -> Path:
    path = manifest_path(bundle_root)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verify_bundle(bundle_root: str | Path) -> tuple[bool, str]:
    root = Path(bundle_root)
    if not root.exists() or not root.is_dir():
        return False, f"bundle directory missing: {root}"

    config_path = root / "config.yaml"
    if not config_path.exists():
        return False, f"bundle config missing: {config_path}"

    manifest_file = manifest_path(root)
    if not manifest_file.exists():
        return False, f"bundle manifest missing: {manifest_file}"

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"bundle manifest invalid: {exc}"

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return False, "bundle manifest missing file entries"

    for entry in files:
        rel_path = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(rel_path, str) or not isinstance(expected, str):
            return False, "bundle manifest contains invalid file entry"
        file_path = root / rel_path
        if not file_path.exists() or not file_path.is_file():
            return False, f"bundle file missing: {rel_path}"
        actual = file_sha256(file_path)
        if actual != expected:
            return False, f"bundle checksum mismatch: {rel_path}"

    refs = extract_pipeline_model_refs(config_path.read_text(encoding="utf-8"))
    for key, value in refs.items():
        if is_huggingface_repo_ref(value):
            return False, f"bundle still references external model for {key}: {value}"
        ref_path = (root / value).resolve()
        try:
            ref_path.relative_to(root.resolve())
        except ValueError:
            return False, f"bundle reference escapes root for {key}: {value}"
        if not ref_path.exists() or not ref_path.is_dir():
            return False, f"bundle dependency missing for {key}: {value}"

    return True, "ok"


def prepare_bundle_from_huggingface(bundle_root: str | Path, *, repo_id: str, hf_token: str) -> tuple[bool, str]:
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    root = Path(bundle_root)
    staging_root = Path(tempfile.mkdtemp(prefix=f"{root.name}-", dir=str(root.parent)))
    deps_root = staging_root / "deps"
    deps_root.mkdir(parents=True, exist_ok=True)

    try:
        snapshot_download(
            repo_id=repo_id,
            token=hf_token,
            local_dir=str(staging_root),
            local_dir_use_symlinks=False,
        )
        config_path = staging_root / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        refs = extract_pipeline_model_refs(config_text)
        replacements: dict[str, str] = {}
        bundled_refs: list[str] = []

        for value in refs.values():
            if not is_huggingface_repo_ref(value):
                continue
            dependency_dir = deps_root / sanitize_repo_id(value)
            snapshot_download(
                repo_id=value,
                token=hf_token,
                local_dir=str(dependency_dir),
                local_dir_use_symlinks=False,
            )
            replacements[value] = (Path("deps") / dependency_dir.name).as_posix()
            bundled_refs.append(value)

        config_path.write_text(
            rewrite_pipeline_refs(config_text, replacements),
            encoding="utf-8",
        )

        bundled_refs.extend(
            copy_compatibility_assets(
                staging_root,
                deps_root,
                repo_id=repo_id,
                hf_token=hf_token,
            )
        )

        remove_cache_metadata(staging_root)

        manifest = build_manifest(
            staging_root,
            metadata={
                "source": "huggingface",
                "primary_repo": repo_id,
                "dependencies": bundled_refs,
            },
        )
        write_manifest(staging_root, manifest)

        if root.exists():
            shutil.rmtree(root)
        shutil.move(str(staging_root), str(root))
        ok, reason = verify_bundle(root)
        return ok, reason
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def create_bundle_archive(bundle_root: str | Path, archive_path: str | Path) -> tuple[Path, str]:
    root = Path(bundle_root)
    archive = Path(archive_path)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(root, arcname=root.name)
    return archive, file_sha256(archive)


def materialize_bundle_from_uri(
    bundle_uri: str,
    target_dir: str | Path,
    *,
    expected_sha256: str = "",
) -> tuple[bool, str]:
    target = Path(target_dir)
    source = urllib.parse.urlparse(bundle_uri)

    if source.scheme in {"", "file"}:
        local_path = Path(source.path if source.scheme == "file" else bundle_uri)
        if local_path.is_dir():
            return _replace_dir(local_path, target)
        return _replace_from_archive(local_path, target, expected_sha256=expected_sha256)

    if source.scheme not in {"http", "https"}:
        return False, f"unsupported bundle URI scheme: {source.scheme}"

    with tempfile.TemporaryDirectory(prefix="pyannote-bundle-download-") as temp_dir:
        archive_name = Path(source.path).name or "pyannote-bundle.tar.gz"
        downloaded = Path(temp_dir) / archive_name
        with urllib.request.urlopen(bundle_uri) as response, downloaded.open("wb") as output:
            shutil.copyfileobj(response, output)
        return _replace_from_archive(downloaded, target, expected_sha256=expected_sha256)


def _replace_dir(source_dir: Path, target_dir: Path) -> tuple[bool, str]:
    staging_dir = target_dir.parent / f".{target_dir.name}.staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    shutil.copytree(source_dir, staging_dir)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.move(str(staging_dir), str(target_dir))
    return verify_bundle(target_dir)


def _replace_from_archive(archive_path: Path, target_dir: Path, *, expected_sha256: str = "") -> tuple[bool, str]:
    if expected_sha256 and file_sha256(archive_path) != expected_sha256:
        return False, f"bundle archive checksum mismatch: {archive_path}"

    with tempfile.TemporaryDirectory(prefix="pyannote-bundle-extract-") as temp_dir:
        temp_root = Path(temp_dir)
        _extract_archive(archive_path, temp_root)
        extracted_root = _resolve_extracted_root(temp_root)
        return _replace_dir(extracted_root, target_dir)


def _extract_archive(archive_path: Path, destination: Path) -> None:
    suffixes = archive_path.suffixes
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract_zip(archive, destination)
        return

    if archive_path.suffix == ".tar" or suffixes[-2:] == [".tar", ".gz"] or archive_path.suffix == ".tgz":
        with tarfile.open(archive_path, "r:*") as archive:
            _safe_extract_tar(archive, destination)
        return

    raise ValueError(f"unsupported bundle archive format: {archive_path}")


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for name in archive.namelist():
        member_path = (destination / name).resolve()
        try:
            member_path.relative_to(destination_resolved)
        except ValueError as exc:
            raise ValueError(f"unsafe archive member path: {name}") from exc
    archive.extractall(destination)


def _safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in archive.getmembers():
        member_path = (destination / member.name).resolve()
        try:
            member_path.relative_to(destination_resolved)
        except ValueError as exc:
            raise ValueError(f"unsafe archive member path: {member.name}") from exc
    archive.extractall(destination)


def _resolve_extracted_root(destination: Path) -> Path:
    children = [child for child in destination.iterdir() if child.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return destination