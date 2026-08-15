from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .msvc import check_sdk_abi, find_visual_studio
from .state import StateStore


@dataclass
class Check:
    name: str
    status: str
    detail: str
    path: str | None = None


def _probe(
    command: str,
    args: list[str],
    minimum: tuple[int, int] | None = None,
    *,
    missing_status: str = "missing",
) -> Check:
    executable = shutil.which(command)
    if not executable:
        return Check(command, missing_status, "not found on PATH")
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(command, "error", str(exc), executable)
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"exit code {result.returncode}"
    status = "ok" if result.returncode == 0 else "error"
    if minimum and status == "ok":
        match = re.search(r"(\d+)\.(\d+)", detail)
        if match and tuple(map(int, match.groups())) < minimum:
            status = "unsupported"
            detail += f"; requires >= {minimum[0]}.{minimum[1]}"
    return Check(command, status, detail, executable)


def run_doctor(store: StateStore) -> list[Check]:
    checks = [
        _probe("cmake", ["--version"], (3, 14)),
        _probe("ctest", ["--version"], (3, 14)),
        _probe("ninja", ["--version"], missing_status="optional"),
        _probe("xmake", ["--version"], missing_status="optional"),
    ]
    visual_studio = find_visual_studio() if os.name == "nt" else None
    if visual_studio:
        checks.append(
            Check(
                "visual-studio",
                "ok" if visual_studio.complete else "optional",
                f"VS {visual_studio.version}; MSVC toolset {visual_studio.toolset_version}"
                + ("; installation/update is incomplete" if not visual_studio.complete else ""),
                str(visual_studio.path),
            )
        )
    compiler_checks = [
        _probe("cl", []),
        _probe("clang++", ["--version"]),
        _probe("g++", ["--version"], (12, 0)),
    ]
    if (
        visual_studio
        and visual_studio.complete
        and visual_studio.dumpbin.with_name("cl.exe").is_file()
        and compiler_checks[0].status == "missing"
    ):
        compiler_checks[0] = Check(
            "cl",
            "ok",
            "available through Visual Studio developer environment",
            str(visual_studio.dumpbin.with_name("cl.exe")),
        )
    available = [check for check in compiler_checks if check.status == "ok"]
    if visual_studio:
        available.append(Check("msvc", "ok", visual_studio.toolset_version))
    if available:
        checks.extend(compiler_checks)
    else:
        checks.extend(compiler_checks)
        checks.append(Check("compiler", "missing", "no C++ compiler found; EUI-NEO explicitly requires GCC 12+ when GCC is used"))
    vulkan_sdk = os.environ.get("VULKAN_SDK")
    validator = shutil.which("glslangValidator")
    if vulkan_sdk or validator:
        checks.append(Check("vulkan", "ok", vulkan_sdk or "glslangValidator available", validator))
    else:
        checks.append(Check("vulkan", "optional", "VULKAN_SDK/glslangValidator not found"))
    try:
        sdk = store.active()
        config = next(iter(sdk.path.rglob("EuiNeoConfig.cmake")), None)
        checks.append(
            Check(
                "eui-sdk",
                "ok" if config else "error",
                f"{sdk.version} ({sdk.platform}-{sdk.arch})",
                str(sdk.path),
            )
        )
        if config and os.name == "nt":
            abi = check_sdk_abi(sdk, visual_studio)
            checks.append(Check("msvc-sdk-abi", abi.status, abi.detail))
    except Exception as exc:
        checks.append(Check("eui-sdk", "optional", str(exc)))
    return checks


def checks_as_dict(checks: list[Check]) -> list[dict[str, str | None]]:
    return [asdict(check) for check in checks]
