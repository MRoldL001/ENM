from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .state import InstalledSdk


@dataclass
class VisualStudio:
    version: str
    path: Path
    toolset_version: str
    toolset_path: Path
    dumpbin: Path
    complete: bool = True


@dataclass
class AbiResult:
    status: str
    detail: str
    missing_symbols: tuple[str, ...] = ()


def _vswhere_path() -> Path | None:
    roots = [
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        r"C:\Program Files (x86)",
    ]
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / "Microsoft Visual Studio/Installer/vswhere.exe"
        if candidate.is_file():
            return candidate
    return None


def find_visual_studio() -> VisualStudio | None:
    vswhere = _vswhere_path()
    if not vswhere:
        return None
    try:
        base = [str(vswhere), "-latest", "-products", "*", "-format", "json", "-utf8"]
        result = subprocess.run(
            [*base, "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        instances = json.loads(result.stdout.decode("utf-8-sig", errors="replace"))
        if not instances:
            result = subprocess.run(
                [str(vswhere), "-all", "-products", "*", "-format", "json", "-utf8"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            instances = json.loads(result.stdout.decode("utf-8-sig", errors="replace"))
        if result.returncode or not instances:
            return None
        instance = instances[0]
        install = Path(instance["installationPath"])
        tools_root = install / "VC/Tools/MSVC"
        toolsets = sorted(
            (entry for entry in tools_root.iterdir() if entry.is_dir()),
            key=lambda entry: tuple(int(part) for part in re.findall(r"\d+", entry.name)),
        )
        if not toolsets:
            return None
        toolset = toolsets[-1]
        dumpbin = toolset / "bin/Hostx64/x64/dumpbin.exe"
        return VisualStudio(
            version=instance.get("catalog", {}).get("productDisplayVersion")
            or instance.get("installationVersion", "unknown"),
            path=install,
            toolset_version=toolset.name,
            toolset_path=toolset,
            dumpbin=dumpbin,
            complete=bool(instance.get("isComplete", True)),
        )
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None


def _dump(dumpbin: Path, option: str, file: Path) -> str:
    result = subprocess.run(
        [str(dumpbin), option, str(file)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise OSError(f"dumpbin failed for {file}")
    return result.stdout.decode("ascii", errors="ignore")


def _undefined_std_symbols(output: str) -> set[str]:
    return set(re.findall(r"UNDEF[^\r\n|]*\|\s*(__std_[A-Za-z0-9_]+)", output))


def _provided_std_symbols(output: str) -> set[str]:
    return set(re.findall(r"\b(__std_[A-Za-z0-9_]+)\b", output))


def check_sdk_abi(sdk: InstalledSdk, visual_studio: VisualStudio | None = None) -> AbiResult:
    if sdk.platform != "windows":
        return AbiResult("optional", "MSVC ABI check is only applicable on Windows")
    visual_studio = visual_studio or find_visual_studio()
    if not visual_studio:
        return AbiResult("unknown", "Visual Studio C++ Build Tools could not be inspected")
    if not visual_studio.dumpbin.is_file():
        return AbiResult(
            "unknown",
            "Visual Studio installation/update is incomplete; dumpbin is not currently available",
        )
    library = next(iter(sdk.path.rglob("eui_neo.lib")), None)
    runtime_dir = visual_studio.toolset_path / "lib/x64"
    runtimes = [
        runtime_dir / name
        for name in ("msvcprt.lib", "vcruntime.lib", "libcmt.lib", "libcpmt.lib")
        if (runtime_dir / name).is_file()
    ]
    if not library or not runtimes:
        return AbiResult("unknown", "SDK or local MSVC runtime library was not found")
    try:
        required = _undefined_std_symbols(_dump(visual_studio.dumpbin, "/symbols", library))
        provided: set[str] = set()
        for runtime in runtimes:
            provided.update(
                _provided_std_symbols(_dump(visual_studio.dumpbin, "/linkermember:2", runtime))
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AbiResult("unknown", f"could not inspect MSVC libraries: {exc}")
    missing = tuple(sorted(required - provided))
    if missing:
        shown = ", ".join(missing[:3])
        suffix = "..." if len(missing) > 3 else ""
        return AbiResult(
            "unsupported",
            f"EUI-NEO {sdk.version} requires MSVC STL symbols unavailable in "
            f"VS {visual_studio.version} / toolset {visual_studio.toolset_version}: "
            f"{shown}{suffix}. Update Visual Studio 2022 Build Tools.",
            missing,
        )
    return AbiResult(
        "ok",
        f"EUI-NEO {sdk.version} is compatible with inspected MSVC STL "
        f"{visual_studio.toolset_version}",
    )
