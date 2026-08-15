from __future__ import annotations

import json
import os
import platform
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .github import (
    Asset,
    Release,
    ReleaseError,
    download_asset,
    extract_archive,
    normalize_arch,
    normalize_platform,
    select_asset,
    verify_digest,
)


def default_home() -> Path:
    override = os.environ.get("ENM_HOME")
    if override:
        return Path(override).expanduser().resolve()
    system = platform.system().lower()
    if system == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / "enm"
    if system == "darwin":
        return Path.home() / "Library/Application Support/enm"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "enm"


@dataclass
class InstalledSdk:
    version: str
    platform: str
    arch: str
    path: Path
    asset: str
    sha256: str

    @property
    def key(self) -> str:
        return f"{self.platform}-{self.arch}"


class StateStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_home()).resolve()
        self.state_file = self.root / "state.json"
        self.sdks_dir = self.root / "sdks"
        self.sources_dir = self.root / "sources"
        self.tmp_dir = self.root / "tmp"

    def ensure(self) -> None:
        self.sdks_dir.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"schema": 1, "active": {}, "installed": {}}
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"invalid tool state at {self.state_file}: {exc}") from exc
        if value.get("schema") != 1:
            raise ReleaseError(f"unsupported tool state schema in {self.state_file}")
        value.setdefault("active", {})
        value.setdefault("installed", {})
        return value

    def save(self, value: dict[str, Any]) -> None:
        self.ensure()
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.state_file)

    def _sdk_path(self, version: str, platform_name: str, arch: str) -> Path:
        if not version.startswith("v") or any(part in version for part in ("/", "\\", "..")):
            raise ReleaseError(f"unsafe SDK version: {version}")
        return self.sdks_dir / version / f"{platform_name}-{arch}"

    def install(
        self,
        release: Release,
        platform_name: str,
        arch: str,
        *,
        force: bool = False,
        allow_unverified: bool = False,
    ) -> InstalledSdk:
        self.ensure()
        asset = select_asset(release, "sdk", platform_name, arch)
        destination = self._sdk_path(release.tag, platform_name, arch)
        if destination.exists() and not force:
            raise ReleaseError(
                f"SDK {release.tag} for {platform_name}-{arch} is already installed; use --force"
            )
        archive_suffix = ".tar.gz" if asset.name.lower().endswith(".tar.gz") else ".zip"
        archive = self.tmp_dir / f"{uuid.uuid4().hex}{archive_suffix}"
        staging = self.tmp_dir / f"sdk-{uuid.uuid4().hex}"
        try:
            download_asset(asset, archive)
            sha256 = verify_digest(archive, asset.digest, allow_unverified=allow_unverified)
            extract_archive(archive, staging)
            configs = list(staging.rglob("EuiNeoConfig.cmake"))
            if not configs:
                raise ReleaseError(f"{asset.name} is not a valid EUI-NEO SDK archive")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                shutil.rmtree(destination)
            staging.replace(destination)
        finally:
            archive.unlink(missing_ok=True)
            if staging.exists():
                shutil.rmtree(staging)
        installed = InstalledSdk(
            version=release.tag,
            platform=platform_name,
            arch=arch,
            path=destination,
            asset=asset.name,
            sha256=sha256,
        )
        state = self.load()
        version_entries = state["installed"].setdefault(release.tag, {})
        version_entries[installed.key] = {
            "path": str(destination),
            "asset": asset.name,
            "sha256": sha256,
            "release_url": release.page_url,
        }
        state["active"][installed.key] = release.tag
        self.save(state)
        return installed

    def get_installed(self, version: str, platform_name: str, arch: str) -> InstalledSdk:
        state = self.load()
        key = f"{platform_name}-{arch}"
        try:
            data = state["installed"][version][key]
        except KeyError as exc:
            raise ReleaseError(f"SDK {version} for {key} is not installed") from exc
        path = Path(data["path"])
        if not path.exists():
            raise ReleaseError(f"installed SDK path no longer exists: {path}")
        return InstalledSdk(version, platform_name, arch, path, data["asset"], data["sha256"])

    def set_active(self, version: str, platform_name: str, arch: str) -> InstalledSdk:
        installed = self.get_installed(version, platform_name, arch)
        state = self.load()
        state["active"][installed.key] = version
        self.save(state)
        return installed

    def active(self, platform_name: str | None = None, arch: str | None = None) -> InstalledSdk:
        platform_name = normalize_platform(platform_name)
        arch = normalize_arch(arch)
        key = f"{platform_name}-{arch}"
        state = self.load()
        version = state["active"].get(key)
        if not version:
            raise ReleaseError(f"no active EUI-NEO SDK for {key}; run 'enm sdk install latest'")
        return self.get_installed(version, platform_name, arch)

    def installed_versions(self) -> dict[str, dict[str, Any]]:
        return self.load()["installed"]

    def uninstall(
        self,
        version: str,
        platform_name: str,
        arch: str,
        *,
        force: bool = False,
    ) -> Path:
        installed = self.get_installed(version, platform_name, arch)
        state = self.load()
        if state["active"].get(installed.key) == version and not force:
            raise ReleaseError(
                f"SDK {version} is active for {installed.key}; switch SDKs first or use --force"
            )
        sdk_root = self.sdks_dir.resolve()
        target = installed.path.resolve()
        try:
            target.relative_to(sdk_root)
        except ValueError as exc:
            raise ReleaseError(f"refusing to remove SDK outside ENM state directory: {target}") from exc
        if target == sdk_root:
            raise ReleaseError("refusing to remove the ENM SDK root")
        shutil.rmtree(target)
        if state["active"].get(installed.key) == version:
            state["active"].pop(installed.key, None)
        version_entries = state["installed"].get(version, {})
        version_entries.pop(installed.key, None)
        if not version_entries:
            state["installed"].pop(version, None)
            version_dir = target.parent
            if version_dir.parent.resolve() == sdk_root and version_dir.exists():
                try:
                    version_dir.rmdir()
                except OSError:
                    pass
        self.save(state)
        return target
