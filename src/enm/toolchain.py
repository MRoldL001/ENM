from __future__ import annotations

import os
import locale
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .msvc import find_visual_studio


# Minimum compiler versions required by EUI-NEO upstream.
MSVC_MINIMUM = (19, 29)  # Visual Studio 2019 16.11
GCC_MINIMUM = (12, 0)
CLANG_MINIMUM = (14, 0)


@dataclass
class Compiler:
    path: Path
    family: str
    version: tuple[int, ...]
    detail: str


@dataclass
class _Comparison:
    op: str
    version: tuple[int, ...]


class VersionConstraint:
    def __init__(self, comparisons: list[_Comparison]) -> None:
        self.comparisons = comparisons

    @classmethod
    def parse(cls, text: str) -> "VersionConstraint":
        comparisons: list[_Comparison] = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            match = re.match(r"^(>=|<=|>|<|=)?\s*(\d+(?:\.\d+)*)$", part)
            if not match:
                raise ValueError(f"invalid version constraint: {part!r}")
            op = match.group(1) or "="
            version = tuple(int(p) for p in match.group(2).split("."))
            comparisons.append(_Comparison(op, version))
        return cls(comparisons)

    def match(self, version: tuple[int, ...]) -> bool:
        for comp in self.comparisons:
            if comp.op == ">=" and not version >= comp.version:
                return False
            if comp.op == "<=" and not version <= comp.version:
                return False
            if comp.op == ">" and not version > comp.version:
                return False
            if comp.op == "<" and not version < comp.version:
                return False
            if comp.op == "=" and version != comp.version:
                return False
        return True

    def __str__(self) -> str:
        parts = []
        for comp in self.comparisons:
            version = ".".join(str(p) for p in comp.version)
            parts.append(f"{comp.op}{version}")
        return ",".join(parts)


def _compiler_family(name: str) -> str:
    lower = name.lower()
    if "cl" in lower and "clang" not in lower:
        return "msvc"
    if "clang" in lower:
        return "clang"
    if "g++" in lower or "gcc" in lower:
        return "gcc"
    return "unknown"


def _compiler_version(path: Path, family: str) -> tuple[tuple[int, ...] | None, str]:
    # MSVC version is most reliably derived from the toolset directory in the path.
    if family == "msvc":
        toolset_match = re.search(r"VC[/\\]Tools[/\\]MSVC[/\\](\d+)\.(\d+)", str(path))
        if toolset_match:
            major, minor = int(toolset_match.group(1)), int(toolset_match.group(2))
            if major == 14:
                return (19, minor), f"MSVC toolset {major}.{minor}"

    args = ["--version"] if family != "msvc" else []
    try:
        result = subprocess.run(
            [str(path), *args],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    if family == "msvc":
        match = re.search(r"Version\s+(\d+)\.(\d+)", output)
        if match:
            return tuple(int(p) for p in match.groups()), output.splitlines()[0]
        return None, output.splitlines()[0] if output else f"exit code {result.returncode}"

    if family == "gcc":
        match = re.search(r"\b(\d+)\.(\d+)(?:\.\d+)?\b", output)
    elif family == "clang":
        match = re.search(r"(?:clang\s+version|version)\s+(\d+)\.(\d+)", output)
    else:
        match = None
    if match:
        return tuple(int(p) for p in match.groups()), output.splitlines()[0]
    return None, output.splitlines()[0] if output else f"exit code {result.returncode}"


def detect_compilers() -> list[Compiler]:
    compilers: list[Compiler] = []

    # On Windows, MSVC may not be on PATH; discover it via Visual Studio.
    if os.name == "nt":
        vs = find_visual_studio()
        if vs is not None:
            cl = vs.path / "VC/Tools/MSVC" / vs.toolset_version / "bin/Hostx64/x64/cl.exe"
            if cl.is_file():
                version, detail = _compiler_version(cl, "msvc")
                compilers.append(Compiler(cl, "msvc", version or (0,), detail))

    for name, expected_family in (("g++", "gcc"), ("clang++", "clang")):
        path = shutil.which(name)
        if not path:
            continue
        family = _compiler_family(name)
        if family != expected_family:
            continue
        version, detail = _compiler_version(Path(path), family)
        compilers.append(Compiler(Path(path), family, version or (0,), detail))
    return compilers


def compiler_meets_minimum(compiler: Compiler) -> bool:
    minimums = {"msvc": MSVC_MINIMUM, "gcc": GCC_MINIMUM, "clang": CLANG_MINIMUM}
    minimum = minimums.get(compiler.family)
    if minimum is None:
        return True
    return compiler.version >= minimum


def resolve_compiler() -> Compiler | None:
    for env_var in ("CXX", "CC"):
        value = os.environ.get(env_var)
        if value:
            path = shutil.which(value)
            if path:
                family = _compiler_family(Path(path).name)
                version, detail = _compiler_version(Path(path), family)
                return Compiler(Path(path), family, version or (0,), detail)
    compilers = detect_compilers()
    return compilers[0] if compilers else None


def resolve_toolchain_compiler(manifest: dict[str, Any]) -> Compiler | None:
    """Return a compiler matching the manifest toolchain constraint, or None."""
    toolchain = manifest.get("toolchain") or {}
    family = toolchain.get("compiler")
    if not family:
        return None
    version_text = toolchain.get("version", "")
    constraint = VersionConstraint.parse(version_text) if version_text else None

    current = resolve_compiler()
    if current is not None and current.family == family:
        if constraint is None or constraint.match(current.version):
            return current

    for compiler in detect_compilers():
        if compiler.family == family and (constraint is None or constraint.match(compiler.version)):
            return compiler
    return None
