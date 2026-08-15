from __future__ import annotations

import hashlib
import json
import os
import platform as host_platform
import re
import shutil
import stat
import tarfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


REPOSITORY = "sudoevolve/EUI-NEO"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
USER_AGENT = "enm/0.1"


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    digest: str | None


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    published_at: str
    prerelease: bool
    assets: tuple[Asset, ...]
    page_url: str


def normalize_platform(value: str | None = None) -> str:
    raw = (value or host_platform.system()).lower()
    aliases = {
        "windows": "windows",
        "win32": "windows",
        "linux": "linux",
        "darwin": "macos",
        "macos": "macos",
        "macosx": "macos",
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ReleaseError(f"unsupported platform: {raw}") from exc


def normalize_arch(value: str | None = None) -> str:
    raw = (value or host_platform.machine()).lower()
    aliases = {
        "amd64": "x64",
        "x86_64": "x64",
        "x64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ReleaseError(f"unsupported architecture: {raw}") from exc


def normalize_tag(value: str) -> str:
    value = value.strip()
    if value == "latest":
        return value
    return value if value.startswith("v") else f"v{value}"


class ReleaseClient:
    def __init__(self, opener: Callable[..., Any] | None = None) -> None:
        self._opener = opener or urllib.request.urlopen

    def _json(self, url: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with self._opener(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"GitHub Releases request failed: {exc}") from exc

    @staticmethod
    def _parse(item: dict[str, Any]) -> Release:
        assets = tuple(
            Asset(
                name=str(asset["name"]),
                url=str(asset["browser_download_url"]),
                size=int(asset.get("size", 0)),
                digest=asset.get("digest"),
            )
            for asset in item.get("assets", [])
        )
        return Release(
            tag=str(item["tag_name"]),
            name=str(item.get("name") or item["tag_name"]),
            published_at=str(item.get("published_at") or ""),
            prerelease=bool(item.get("prerelease", False)),
            assets=assets,
            page_url=str(item.get("html_url") or ""),
        )

    def list_releases(self, include_prerelease: bool = False) -> list[Release]:
        releases: list[Release] = []
        for page in range(1, 11):
            items = self._json(f"{API_ROOT}/releases?per_page=100&page={page}")
            if not isinstance(items, list):
                raise ReleaseError("GitHub Releases API returned an unexpected response")
            for item in items:
                if item.get("draft"):
                    continue
                release = self._parse(item)
                if include_prerelease or not release.prerelease:
                    releases.append(release)
            if len(items) < 100:
                break
        return releases

    def get_release(self, version: str, include_prerelease: bool = False) -> Release:
        version = normalize_tag(version)
        if version == "latest":
            releases = self.list_releases(include_prerelease=include_prerelease)
            if not releases:
                raise ReleaseError("no suitable EUI-NEO release was found")
            return releases[0]
        try:
            item = self._json(f"{API_ROOT}/releases/tags/{version}")
        except ReleaseError as exc:
            raise ReleaseError(f"EUI-NEO release {version} was not found") from exc
        release = self._parse(item)
        if release.prerelease and not include_prerelease:
            raise ReleaseError(
                f"{version} is a prerelease; pass --include-prerelease to use it"
            )
        return release


def select_asset(release: Release, kind: str, platform_name: str, arch: str) -> Asset:
    marker = f"-{platform_name}-{arch}-{kind}."
    matches = [asset for asset in release.assets if marker in asset.name.lower()]
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(asset.name for asset in release.assets) or "none"
    if not matches:
        raise ReleaseError(
            f"release {release.tag} has no {kind} asset for {platform_name}-{arch}; "
            f"available assets: {available}"
        )
    raise ReleaseError(f"release {release.tag} contains ambiguous assets: {available}")


def download_asset(asset: Asset, destination: Path) -> None:
    parsed = urlparse(asset.url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ReleaseError(f"refusing non-GitHub release URL: {asset.url}")
    request = urllib.request.Request(
        asset.url,
        headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReleaseError(f"failed to download {asset.name}: {exc}") from exc


def verify_digest(path: Path, digest: str | None, allow_unverified: bool = False) -> str:
    actual = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            actual.update(chunk)
    actual_hex = actual.hexdigest()
    if not digest:
        if not allow_unverified:
            raise ReleaseError(
                "release asset has no digest; pass --allow-unverified to accept it"
            )
        return actual_hex
    algorithm, separator, expected = digest.partition(":")
    if separator != ":" or algorithm.lower() != "sha256":
        raise ReleaseError(f"unsupported release digest: {digest}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ReleaseError(f"invalid SHA-256 digest in release metadata: {digest}")
    if actual_hex.lower() != expected.lower():
        raise ReleaseError(
            f"SHA-256 mismatch for {path.name}: expected {expected}, got {actual_hex}"
        )
    return actual_hex


def _safe_destination(root: Path, member_name: str) -> Path:
    member_name = member_name.replace("\\", "/")
    if member_name.startswith("/") or re.match(r"^[A-Za-z]:", member_name):
        raise ReleaseError(f"archive contains an absolute path: {member_name}")
    destination = (root / member_name).resolve()
    root_resolved = root.resolve()
    try:
        destination.relative_to(root_resolved)
    except ValueError as exc:
        raise ReleaseError(f"archive path escapes destination: {member_name}") from exc
    return destination


def extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if archive.name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                _safe_destination(destination, member.filename)
                unix_mode = member.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise ReleaseError(f"archive contains a symbolic link: {member.filename}")
            package.extractall(destination)
        return
    if archive.name.lower().endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as package:
            for member in package.getmembers():
                _safe_destination(destination, member.name)
                if member.issym() or member.islnk() or member.isdev():
                    raise ReleaseError(f"archive contains an unsafe entry: {member.name}")
            package.extractall(destination)
        return
    raise ReleaseError(f"unsupported release archive: {archive.name}")
