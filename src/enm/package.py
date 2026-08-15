from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import zipfile
from pathlib import Path

from .github import ReleaseError, normalize_arch, normalize_platform
from .project import load_manifest, project_sdk
from .state import StateStore


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _binary_candidates(root: Path, target: str) -> list[Path]:
    build = root / "build/default"
    names = [f"{target}.exe", target]
    candidates: list[Path] = []
    for name in names:
        candidates.extend(build.glob(f"**/{name}"))
    return [
        candidate
        for candidate in candidates
        if candidate.is_file() and "CMakeFiles" not in candidate.parts
    ]


def _find_binary(root: Path, target: str, explicit: Path | None = None) -> Path:
    if explicit:
        result = explicit.resolve()
        if not result.is_file():
            raise ReleaseError(f"application binary does not exist: {result}")
        return result
    candidates = _binary_candidates(root, target)
    if not candidates:
        raise ReleaseError(f"could not find built target '{target}'; run 'enm build' first")
    candidates.sort(key=lambda item: ("Release" not in item.parts, len(item.parts)))
    return candidates[0]


def deploy(
    root: Path,
    store: StateStore,
    destination: Path | None = None,
    binary: Path | None = None,
    force: bool = False,
) -> Path:
    manifest = load_manifest(root)
    sdk = project_sdk(root, store)
    target = manifest["target"]
    binary_path = _find_binary(root, target, binary)
    platform_name = normalize_platform()
    arch = normalize_arch()
    destination = destination or root / "dist" / f"{target}-{sdk.version}-{platform_name}-{arch}"
    destination = destination.resolve()
    dist_root = (root / "dist").resolve()
    if not _within(destination, dist_root):
        raise ReleaseError(f"deployment destination must stay under {dist_root}")
    if destination.exists():
        if not force:
            raise ReleaseError(f"deployment destination already exists: {destination}; use --force")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(binary_path, destination / binary_path.name)
    for pattern in ("*.dll", "*.so", "*.dylib"):
        for library in binary_path.parent.glob(pattern):
            shutil.copy2(library, destination / library.name)
    assets = binary_path.parent / "assets"
    if assets.is_dir():
        shutil.copytree(assets, destination / "assets")
    licenses = destination / "licenses"
    for license_file in root.glob("LICENSE*"):
        licenses.mkdir(exist_ok=True)
        shutil.copy2(license_file, licenses / f"application-{license_file.name}")
    for license_file in sdk.path.glob("LICENSE*"):
        licenses.mkdir(exist_ok=True)
        shutil.copy2(license_file, licenses / f"eui-neo-{license_file.name}")
    metadata = {
        "schema": 1,
        "application": manifest["name"],
        "target": target,
        "eui_version": sdk.version,
        "platform": platform_name,
        "arch": arch,
        "sdk_sha256": sdk.sha256,
        "binary": binary_path.name,
    }
    (destination / "enm-package.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    return destination


def package_stage(stage: Path, format_name: str) -> tuple[Path, Path]:
    stage = stage.resolve()
    dist = stage.parent
    if format_name == "zip":
        archive = dist / f"{stage.name}.zip"
    elif format_name == "tar.gz":
        archive = dist / f"{stage.name}.tar.gz"
    else:
        raise ReleaseError(f"unsupported package format: {format_name}")
    temporary = archive.with_name(archive.name + ".tmp")
    try:
        if format_name == "zip":
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as out:
                for path in sorted(stage.rglob("*")):
                    if path.is_file():
                        out.write(path, Path(stage.name) / path.relative_to(stage))
        else:
            with tarfile.open(temporary, "w:gz") as out:
                out.add(stage, arcname=stage.name)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{digest.hexdigest()}  {archive.name}\n", encoding="ascii")
    return archive, sidecar


def remove_packaged_stage(stage: Path, dist_root: Path) -> None:
    stage = stage.resolve()
    dist_root = dist_root.resolve()
    if not stage.is_dir() or stage.parent != dist_root or stage == dist_root:
        raise ReleaseError(f"refusing to remove invalid package staging directory: {stage}")
    shutil.rmtree(stage)
