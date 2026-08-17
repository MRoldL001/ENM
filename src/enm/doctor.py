from __future__ import annotations

import os
import locale
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .github import ReleaseError, normalize_arch, normalize_platform
from .msvc import check_sdk_abi, find_visual_studio
from .project import _legacy_source, has_configure_helper, load_manifest
from .state import StateStore
from .toolchain import (
    CLANG_MINIMUM,
    Compiler,
    GCC_MINIMUM,
    MSVC_MINIMUM,
    compiler_meets_minimum,
    detect_compilers,
    resolve_compiler,
    resolve_toolchain_compiler,
)
from .ui import GREEN, RED, RESET, Spinner


@dataclass
class Check:
    name: str
    status: str
    detail: str
    path: str | None = None
    required: bool = True
    parent: str | None = None


# EUI-NEO upstream requirements (from README and CMakeLists.txt)
CMAKE_MINIMUM = (3, 14)
VS_MINIMUM = (16, 11)  # Visual Studio 2019 16.11


def _probe(
    command: str,
    args: list[str],
    minimum: tuple[int, int] | None = None,
    *,
    missing_status: str = "missing",
) -> Check:
    is_optional = missing_status == "optional"
    executable = shutil.which(command)
    if not executable:
        return Check(command, missing_status, "not found on PATH", required=not is_optional)
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(command, "error", str(exc), executable, required=not is_optional)
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"exit code {result.returncode}"
    status = "ok" if result.returncode == 0 else "error"
    if minimum and status == "ok":
        match = re.search(r"(\d+)\.(\d+)", detail)
        if match and tuple(map(int, match.groups())) < minimum:
            status = "unsupported"
            detail += f"; requires >= {minimum[0]}.{minimum[1]}"
    return Check(command, status, detail, executable, required=not is_optional)


def _parse_version(output: str, pattern: str) -> tuple[int, ...] | None:
    match = re.search(pattern, output)
    if not match:
        return None
    return tuple(int(part) for part in re.findall(r"\d+", match.group(0)))


def _minimum_label(minimum: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in minimum)


def _tool_versions(project_root: Path | None = None) -> tuple[Check, ...]:
    ninja_required = False
    if project_root is not None and os.name == "nt":
        try:
            manifest = load_manifest(project_root)
            toolchain = manifest.get("toolchain") or {}
            compiler_family = toolchain.get("compiler")
            if compiler_family and compiler_family != "msvc":
                ninja_required = True
        except ReleaseError:
            pass

    checks = [
        _probe("cmake", ["--version"], CMAKE_MINIMUM),
        _probe("ctest", ["--version"], CMAKE_MINIMUM),
    ]
    if ninja_required:
        checks.append(_probe("ninja", ["--version"]))
    elif project_root is None:
        checks.append(_probe("ninja", ["--version"], missing_status="optional"))
    if project_root is None:
        checks.append(_probe("xmake", ["--version"], missing_status="optional"))
    return tuple(checks)


def _find_compiler() -> tuple[Path, str] | tuple[None, None]:
    """Return the C++ compiler ENM/CMake is most likely to use and its family."""
    for env_var in ("CXX", "CC"):
        value = os.environ.get(env_var)
        if value and shutil.which(value):
            path = Path(value)
            family = _compiler_family(path.name)
            return path, family
    if os.name == "nt":
        cl = shutil.which("cl")
        if cl:
            return Path(cl), "msvc"
    for name, family in (("g++", "gcc"), ("clang++", "clang")):
        path = shutil.which(name)
        if path:
            return Path(path), family
    return None, None


def _find_msvc_from_vs(vs) -> Path | None:
    """Locate cl.exe from a Visual Studio installation object."""
    if not vs:
        return None
    cl = vs.path / "VC/Tools/MSVC" / vs.toolset_version / "bin/Hostx64/x64/cl.exe"
    if cl.is_file():
        return cl
    # Fallback: search under the toolset directory
    for candidate in (vs.path / "VC/Tools/MSVC").rglob("cl.exe"):
        if "x64" in str(candidate):
            return candidate
    return None


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
        # cl.exe emits version info to stderr and exits 0 only in certain modes
        version = _parse_version(output, r"Version\s+\d+\.\d+")
    elif family == "gcc":
        version = _parse_version(output, r"\bg\+\+\s+.*\d+\.\d+")
    elif family == "clang":
        version = _parse_version(output, r"clang\s+version\s+\d+\.\d+")
    else:
        version = None
    if version:
        return version, output.splitlines()[0]
    return None, output.splitlines()[0] if output else f"exit code {result.returncode}"


def _minimum_for_family(family: str) -> tuple[int, int] | None:
    return {
        "msvc": MSVC_MINIMUM,
        "gcc": GCC_MINIMUM,
        "clang": CLANG_MINIMUM,
    }.get(family)


def _cpp17_probe(path: Path, family: str, temp_root: Path | None = None, visual_studio=None) -> Check:
    code = (
        '#include <optional>\n'
        '#include <string_view>\n'
        'int main() {\n'
        '    std::optional<int> value = 17;\n'
        '    std::string_view text = "ok";\n'
        '    return value.value_or(0) == 17 && text.size() == 2 ? 0 : 1;\n'
        '}\n'
    )
    with tempfile.TemporaryDirectory(prefix="enm-doctor-", dir=temp_root) as directory:
        source = Path(directory) / "probe.cpp"
        source.write_text(code, encoding="utf-8")
        output = Path(directory) / ("probe.exe" if os.name == "nt" else "probe")
        if family == "msvc":
            # cl.exe needs the VS developer environment. Use vcvarsall.bat if available.
            # Explicit object path keeps probe.obj inside the temp directory.
            obj = output.with_suffix(".obj")
            vcvarsall = visual_studio.path / "VC/Auxiliary/Build/vcvarsall.bat" if visual_studio else None
            if vcvarsall and vcvarsall.is_file():
                command = f'"{vcvarsall}" x64 && "{path}" /nologo /EHsc /std:c++17 "{source}" /Fo:"{obj}" /Fe:"{output}"'
                shell = True
            else:
                command = [str(path), "/nologo", "/EHsc", "/std:c++17", str(source), f"/Fo:{obj}", f"/Fe:{output}"]
                shell = False
        else:
            command = [str(path), "-std=c++17", str(source), "-o", str(output)]
            shell = False
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors="replace",
                timeout=30,
                check=False,
                shell=shell,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Check("c++17", "error", f"probe failed: {exc}", str(path))
        if result.returncode == 0 and output.is_file():
            return Check("c++17", "ok", "compiler supports C++17", str(path))
        detail = (result.stderr or result.stdout or "probe did not compile").strip().splitlines()[0]
        return Check("c++17", "unsupported", detail, str(path))


def _compiler_check(
    temp_root: Path | None = None,
    visual_studio=None,
    project_root: Path | None = None,
) -> list[Check]:
    # On Windows ENM uses the Visual Studio generator, so check MSVC even when
    # another compiler (e.g. MinGW) happens to be first on PATH.
    path: Path | None = None
    family: str | None = None
    if project_root is not None:
        try:
            manifest = load_manifest(project_root)
            selected = resolve_toolchain_compiler(manifest)
        except (ReleaseError, ValueError):
            selected = None
        if selected is not None:
            path, family = selected.path, selected.family
    if path is None and os.name == "nt" and visual_studio:
        path = _find_msvc_from_vs(visual_studio)
        if path:
            family = "msvc"
    if path is None:
        path, family = _find_compiler()
    if path is None:
        return [
            Check("compiler", "missing", "no C++ compiler found; EUI-NEO requires MSVC 19.29+, GCC 12+, or Clang 14+", required=True)
        ]
    version, detail = _compiler_version(path, family)
    minimum = _minimum_for_family(family)
    checks: list[Check] = []
    checks.append(_cpp17_probe(path, family, temp_root=temp_root, visual_studio=visual_studio))
    return checks


def _compiler_family_checks(selected: Compiler | None = None) -> list[Check]:
    """Return a compiler inventory: one parent check plus one child per family."""
    by_family: dict[str, Compiler] = {c.family: c for c in detect_compilers()}
    children: list[Check] = []
    for family in ("msvc", "gcc", "clang"):
        compiler = by_family.get(family)
        if compiler is None:
            children.append(
                Check(
                    family,
                    "missing",
                    f"{family} compiler not found",
                    required=False,
                    parent="compiler",
                )
            )
            continue
        label = ".".join(str(part) for part in compiler.version)
        if compiler_meets_minimum(compiler):
            is_selected = bool(
                selected
                and selected.family == compiler.family
                and selected.path.resolve() == compiler.path.resolve()
            )
            children.append(
                Check(
                    family,
                    "ok",
                    label,
                    str(compiler.path),
                    required=is_selected,
                    parent="compiler",
                )
            )
        else:
            minimums = {"msvc": MSVC_MINIMUM, "gcc": GCC_MINIMUM, "clang": CLANG_MINIMUM}
            minimum = minimums[family]
            children.append(
                Check(
                    family,
                    "unsupported",
                    f"{label} requires >= {_minimum_label(minimum)}",
                    str(compiler.path),
                    required=False,
                    parent="compiler",
                )
            )

    if any(c.status == "ok" for c in children):
        parent_status = "ok"
        parent_detail = (
            f"selected compiler: {selected.family} {'.'.join(str(part) for part in selected.version)}"
            if selected else "at least one supported compiler found"
        )
        # Once any compiler is usable, others are purely optional.
        for c in children:
            if c.status != "ok" and not c.required:
                c.status = "optional"
                c.detail = "not required because another compiler is available"
    elif any(c.status == "unsupported" for c in children):
        parent_status = "unsupported"
        parent_detail = "no compiler meets the minimum version requirement"
    else:
        parent_status = "missing"
        parent_detail = "no supported compiler found"
    parent = Check("compiler", parent_status, parent_detail, required=True)
    return [parent, *children]



def _toolchain_constraint_checks(project_root: Path) -> list[Check]:
    """Validate that a compiler satisfying the manifest toolchain constraint is available."""
    try:
        manifest = load_manifest(project_root)
    except ReleaseError:
        return []
    toolchain = manifest.get("toolchain") or {}
    family = toolchain.get("compiler")
    if not family:
        return []

    version_text = toolchain.get("version", "")
    compiler = resolve_toolchain_compiler(manifest)
    if compiler is not None:
        current = ".".join(str(part) for part in compiler.version)
        detail = f"{compiler.family} {current} matches the project constraint {version_text}".strip()
        if os.name == "nt" and compiler.family != "msvc" and not shutil.which("ninja"):
            return [
                Check(
                    "toolchain",
                    "missing",
                    f"{detail}, but Ninja is required for {compiler.family} builds on Windows",
                    str(compiler.path),
                    required=True,
                )
            ]
        return [
            Check(
                "toolchain",
                "ok",
                detail,
                str(compiler.path),
                required=True,
            )
        ]

    active = resolve_compiler()
    if active is None:
        return [
            Check(
                "toolchain",
                "missing",
                f"project requires {family} {version_text}".strip() + " but no compiler was found",
                required=True,
            )
        ]
    if active.family != family:
        return [
            Check(
                "toolchain",
                "unsupported",
                f"project requires {family} {version_text}".strip() + f" but current compiler is {active.family}",
                str(active.path),
                required=True,
            )
        ]

    current = ".".join(str(part) for part in active.version)
    return [
        Check(
            "toolchain",
            "unsupported",
            f"project requires {family} {version_text} but current is {active.family} {current}",
            str(active.path),
            required=True,
        )
    ]


def _visual_studio_check(vs=None) -> list[Check]:
    if os.name != "nt":
        return []
    if vs is None:
        vs = find_visual_studio()
    if not vs:
        return [Check("visual-studio", "missing", "Visual Studio 2019 16.11+ or 2022 with C++ desktop workload not found", required=True)]
    checks = [
        Check(
            "visual-studio",
            "ok" if vs.complete else "optional",
            f"VS {vs.version}; MSVC toolset {vs.toolset_version}"
            + ("; installation/update is incomplete" if not vs.complete else ""),
            str(vs.path),
        )
    ]
    # Check VS version meets EUI-NEO minimum
    version_tuple = tuple(int(part) for part in re.findall(r"\d+", vs.version))
    if version_tuple[:2] < VS_MINIMUM:
        checks.append(
            Check(
                "vs-version",
                "unsupported",
                f"Visual Studio {vs.version} is older than required {VS_MINIMUM[0]}.{VS_MINIMUM[1]}; update Visual Studio",
                str(vs.path),
            )
        )
    else:
        checks.append(Check("vs-version", "ok", f"Visual Studio {vs.version} meets minimum {VS_MINIMUM[0]}.{VS_MINIMUM[1]}", str(vs.path)))
    return checks


def _pkg_config_exists(module: str) -> bool:
    pkg_config = shutil.which("pkg-config")
    if not pkg_config:
        return False
    try:
        result = subprocess.run(
            [pkg_config, "--exists", module],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pkg_config_version(module: str) -> str | None:
    pkg_config = shutil.which("pkg-config")
    if not pkg_config:
        return None
    try:
        result = subprocess.run(
            [pkg_config, "--modversion", module],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _is_darwin() -> bool:
    return os.name == "posix" and os.uname().sysname == "Darwin"


def _opengl_check() -> Check:
    if os.name == "nt":
        return Check("opengl", "ok", "OpenGL is provided by the Windows SDK")
    if _is_darwin():
        framework = Path("/System/Library/Frameworks/OpenGL.framework")
        if framework.exists():
            return Check("opengl", "ok", "OpenGL framework found", str(framework))
        return Check("opengl", "missing", "OpenGL framework not found; install Xcode Command Line Tools", required=True)
    # Linux / other Unix
    version = _pkg_config_version("gl")
    if version:
        return Check("opengl", "ok", f"OpenGL {version} via pkg-config")
    return Check("opengl", "missing", "OpenGL development files not found; install mesa/libgl1-mesa-dev", required=True)


def _is_linux() -> bool:
    return os.name == "posix" and os.uname().sysname != "Darwin"


def _linux_system_deps() -> list[Check]:
    if not _is_linux():
        return []
    required_modules = {
        "x11": "libx11-dev",
        "xrandr": "libxrandr-dev",
        "xinerama": "libxinerama-dev",
        "xcursor": "libxcursor-dev",
        "xi": "libxi-dev",
        "gl": "libgl1-mesa-dev",
        "libcurl": "libcurl4-openssl-dev",
    }
    checks: list[Check] = []
    for module, package in required_modules.items():
        version = _pkg_config_version(module)
        if version:
            checks.append(Check(module, "ok", f"{version}"))
        else:
            checks.append(Check(module, "missing", f"{module} not found; install {package}", required=True))
    # Tray backend: either glib/gio (SNI, preferred) or GTK3 + libappindicator
    gio_version = _pkg_config_version("gio-2.0")
    appindicator_version = _pkg_config_version("appindicator3-0.1")
    gtk3_version = _pkg_config_version("gtk+-3.0")
    if gio_version:
        checks.append(Check("tray-backend", "ok", f"glib/gio {gio_version} (StatusNotifierItem tray)"))
    elif appindicator_version and gtk3_version:
        checks.append(Check("tray-backend", "ok", f"GTK3 + libappindicator {appindicator_version} (legacy tray)"))
    else:
        checks.append(
            Check(
                "tray-backend",
                "optional",
                "neither glib/gio nor GTK3+libappindicator found; tray support will fail unless disabled with -DEUI_ENABLE_TRAY=OFF",
                required=False,
            )
        )
    return checks


def _vulkan_check(required: bool) -> Check:
    vulkan_sdk = os.environ.get("VULKAN_SDK")
    if vulkan_sdk and Path(vulkan_sdk).is_dir():
        return Check("vulkan", "ok", f"VULKAN_SDK={vulkan_sdk}")
    if shutil.which("glslangValidator"):
        return Check("vulkan", "ok", "glslangValidator available")
    if os.name == "posix" and _pkg_config_exists("vulkan"):
        version = _pkg_config_version("vulkan")
        return Check("vulkan", "ok", f"Vulkan {version} via pkg-config")
    status = "missing" if required else "optional"
    return Check("vulkan", status, "Vulkan SDK not found", required=required)


def _sdl2_check(required: bool) -> Check:
    if shutil.which("sdl2-config"):
        return Check("sdl2", "ok", "sdl2-config available")
    if os.name == "posix" and _pkg_config_exists("sdl2"):
        version = _pkg_config_version("sdl2")
        return Check("sdl2", "ok", f"SDL2 {version} via pkg-config")
    # Windows: look for SDL2 headers in common locations
    for candidate in (
        Path(os.environ.get("SDL2", "")) if os.environ.get("SDL2") else None,
        Path(r"C:\SDL2") if os.name == "nt" else None,
    ):
        if candidate and candidate.is_dir() and (candidate / "include/SDL.h").is_file():
            return Check("sdl2", "ok", f"SDL2 found at {candidate}", str(candidate))
    status = "missing" if required else "optional"
    return Check("sdl2", status, "SDL2 not found; install SDL2 or allow EUI-NEO to fetch it", required=required)


def _network_check(fetch_mode: bool) -> Check:
    try:
        request = urllib.request.Request(
            "https://github.com",
            headers={"User-Agent": "enm-doctor"},
        )
        with urllib.request.urlopen(request, timeout=5):
            return Check("network", "ok", "can reach GitHub")
    except (OSError, urllib.error.URLError, TimeoutError):
        pass
    status = "missing" if fetch_mode else "optional"
    return Check("network", status, "cannot reach github.com; required when EUI_DEPS_MODE=fetch or legacy source download is needed", required=fetch_mode)


def _read_cmake_cache(project_root: Path) -> dict[str, str]:
    """Parse CMakeCache.txt for EUI-NEO option values."""
    cache: dict[str, str] = {}
    try:
        manifest = load_manifest(project_root)
        build_dir = project_root / manifest.get("build_dir", "build/default")
        cache_file = build_dir / "CMakeCache.txt"
        if not cache_file.is_file():
            return cache
        text = cache_file.read_text(encoding="utf-8", errors="ignore")
    except (ReleaseError, OSError):
        return cache
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        match = re.match(r"^(EUI_[A-Z_]+):\w+=(.*)$", line)
        if match:
            cache[match.group(1)] = match.group(2).strip()
    return cache


def _scan_cmake_options(project_root: Path) -> dict[str, str]:
    """Scan CMakeLists.txt and project .cmake files for EUI-NEO backend/deps options."""
    options: dict[str, str] = {}
    files: list[Path] = []
    cmake = project_root / "CMakeLists.txt"
    if cmake.is_file():
        files.append(cmake)
    try:
        files.extend(project_root.rglob("*.cmake"))
    except OSError:
        pass
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name in ("EUI_WINDOW_BACKEND", "EUI_RENDER_BACKEND", "EUI_DEPS_MODE"):
            pattern = rf"\bset\s*\(\s*{re.escape(name)}\s+['\"]?([^'\"\)\s]+)"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                options[name] = match.group(1).strip().lower()
    return options


def _detect_backends(project_root: Path | None) -> tuple[str, str, bool]:
    """Return (window_backend, render_backend, fetch_mode) from project files or defaults."""
    window_backend = "glfw"
    render_backend = "opengl"
    fetch_mode = False
    if not project_root:
        return window_backend, render_backend, fetch_mode

    cache = _read_cmake_cache(project_root)
    options = _scan_cmake_options(project_root)
    options.update(cache)

    window = options.get("EUI_WINDOW_BACKEND", "")
    render = options.get("EUI_RENDER_BACKEND", "")
    deps_mode = options.get("EUI_DEPS_MODE", "")

    if window in {"glfw", "sdl2"}:
        window_backend = window
    elif "sdl2" in window:
        window_backend = "sdl2"

    if render in {"opengl", "vulkan"}:
        render_backend = render
    elif "vulkan" in render or render.startswith("vk"):
        render_backend = "vulkan"

    fetch_mode = deps_mode == "fetch"

    # Legacy fallback: infer from build directory name when no CMake option is present.
    if "EUI_WINDOW_BACKEND" not in options or "EUI_RENDER_BACKEND" not in options:
        try:
            manifest = load_manifest(project_root)
            build_dir_name = Path(manifest.get("build_dir", "build/default")).name.lower()
        except ReleaseError:
            build_dir_name = "default"
        if "sdl2" in build_dir_name and "EUI_WINDOW_BACKEND" not in options:
            window_backend = "sdl2"
        if ("vk" in build_dir_name or "vulkan" in build_dir_name) and "EUI_RENDER_BACKEND" not in options:
            render_backend = "vulkan"

    return window_backend, render_backend, fetch_mode


def _sdk_check(store: StateStore, project_root: Path | None, deep: bool, temp_root: Path | None = None) -> list[Check]:
    checks: list[Check] = []
    sdk = None
    manifest = None
    if project_root:
        try:
            manifest = load_manifest(project_root)
        except ReleaseError as exc:
            checks.append(Check("eui-sdk", "error", f"cannot read project manifest: {exc}", required=True))
            return checks
        version = manifest.get("eui", {}).get("version")
        if not version:
            checks.append(Check("eui-sdk", "error", f"{project_root / 'enm-project.json'} does not pin an EUI-NEO version", required=True))
            return checks
        platform = normalize_platform()
        arch = normalize_arch()
        try:
            sdk = store.get_installed(version, platform, arch)
        except ReleaseError:
            checks.append(
                Check(
                    "eui-sdk",
                    "missing",
                    f"EUI-NEO {version} ({platform}-{arch}) is required by the project but not installed; run 'enm sdk install {version}'",
                    str(project_root),
                    required=True,
                )
            )
            return checks
    if sdk is None:
        try:
            sdk = store.active()
        except ReleaseError as exc:
            checks.append(Check("eui-sdk", "missing", str(exc), required=True))
            return checks

    config = next(iter(sdk.path.rglob("EuiNeoConfig.cmake")), None)
    if not config:
        checks.append(
            Check(
                "eui-sdk",
                "error",
                f"{sdk.version} ({sdk.platform}-{sdk.arch}) does not contain EuiNeoConfig.cmake",
                str(sdk.path),
            )
        )
        return checks

    header = next(iter(sdk.path.rglob("eui_neo.h")), None)
    library = next(iter(sdk.path.rglob("eui_neo.lib")), None) or next(iter(sdk.path.rglob("libeui_neo.a")), None)
    detail_parts = [f"{sdk.version} ({sdk.platform}-{sdk.arch})"]
    if header:
        detail_parts.append("headers present")
    else:
        detail_parts.append("headers missing")
    if library:
        detail_parts.append("library present")
    else:
        detail_parts.append("library missing")

    # Verify exported entry point
    configure_helper_available = False
    has_eui_neo_target = False
    for cmake_file in sdk.path.rglob("*.cmake"):
        try:
            content = cmake_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"(?:function|macro)\s*\(\s*eui_neo_configure_app\b", content, re.IGNORECASE):
            configure_helper_available = True
        if "eui::neo" in content:
            has_eui_neo_target = True
    if configure_helper_available:
        detail_parts.append("eui_neo_configure_app() available")
    elif has_eui_neo_target:
        detail_parts.append("eui::neo target available")
    else:
        detail_parts.append("no supported entry point")

    status = "ok" if (header and library and (configure_helper_available or has_eui_neo_target)) else "error"
    checks.append(Check("eui-sdk", status, "; ".join(detail_parts), str(sdk.path)))

    if sdk.platform == "windows" and status == "ok":
        abi = check_sdk_abi(sdk)
        checks.append(
            Check(
                "msvc-sdk-abi",
                abi.status,
                abi.detail,
                str(sdk.path),
                required=abi.status not in {"optional", "unknown"},
            )
        )

    if deep and status == "ok":
        compiler = (resolve_toolchain_compiler(manifest) if manifest else None) or resolve_compiler()
        legacy_source = None
        if not configure_helper_available:
            try:
                legacy_source = _legacy_source(store, sdk.version)
            except ReleaseError as exc:
                checks.append(Check("sdk-toolchain", "error", str(exc), str(sdk.path)))
                return checks
        checks.append(
            _deep_sdk_probe(
                sdk,
                config,
                compiler=compiler,
                temp_root=temp_root,
                legacy_source=legacy_source,
            )
        )

    return checks


def _deep_sdk_probe(
    sdk,
    config: Path,
    compiler: Compiler | None = None,
    temp_root: Path | None = None,
    legacy_source: Path | None = None,
) -> Check:
    """Configure and link a minimal application against the SDK."""
    with tempfile.TemporaryDirectory(prefix="enm-doctor-sdk-", dir=temp_root) as directory:
        environment = os.environ.copy()
        if os.name == "nt":
            path_value = next((value for key, value in environment.items() if key.lower() == "path"), "")
            environment = {key: value for key, value in environment.items() if key.lower() != "path"}
            environment["Path"] = path_value

        def failure_excerpt(output: str, fallback: str) -> str:
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            index = next(
                (i for i, line in enumerate(lines) if "error" in line.lower() or "fatal" in line.lower()),
                max(0, len(lines) - 1),
            )
            return " | ".join(lines[index:index + 4]) if lines else fallback

        root = Path(directory)
        (root / "probe.cpp").write_text(
            '#include "eui_neo.h"\n'
            "namespace app {\n"
            "const DslAppConfig& dslAppConfig() {\n"
            "    static const DslAppConfig config = DslAppConfig{}\n"
            '        .title("probe")\n'
            '        .pageId("probe")\n'
            "        .windowSize(1, 1);\n"
            "    return config;\n"
            "}\n"
            "void compose(eui::Ui&, const eui::Screen&) {}\n"
            "} // namespace app\n",
            encoding="utf-8",
        )
        legacy_cmake = ""
        if legacy_source is not None:
            source = legacy_source.as_posix()
            legacy_cmake = (
                f'    set(_enm_legacy_source "{source}")\n'
                '    get_target_property(_enm_defs eui::neo INTERFACE_COMPILE_DEFINITIONS)\n'
                '    set(_enm_entry "${_enm_legacy_source}/core/app/glfw_app_main.cpp")\n'
                '    if("${_enm_defs}" MATCHES "EUI_WINDOW_BACKEND_SDL2")\n'
                '        set(_enm_entry "${_enm_legacy_source}/core/app/sdl2_app_main.cpp")\n'
                '    endif()\n'
                '    target_sources(probe PRIVATE "${_enm_entry}")\n'
                '    target_include_directories(probe PRIVATE "${_enm_legacy_source}")\n'
            )
        (root / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.14)\n"
            "project(enm_doctor_probe LANGUAGES C CXX)\n"
            "set(CMAKE_CXX_STANDARD 17)\n"
            "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
            f'find_package(EuiNeo CONFIG REQUIRED PATHS "{config.parent.parent.parent.parent.as_posix()}" NO_DEFAULT_PATH)\n'
            "add_executable(probe probe.cpp)\n"
            "if(COMMAND eui_neo_configure_app)\n"
            "    eui_neo_configure_app(probe)\n"
            "else()\n"
            + legacy_cmake +
            "    target_link_libraries(probe PRIVATE eui::neo)\n"
            "endif()\n",
            encoding="utf-8",
        )
        build_dir = root / "build"
        command = [
            "cmake",
            "-S", str(root),
            "-B", str(build_dir),
            f"-DCMAKE_PREFIX_PATH={sdk.path.as_posix()}",
        ]
        if compiler is not None and not (os.name == "nt" and compiler.family == "msvc"):
            command.append(f"-DCMAKE_CXX_COMPILER={compiler.path.as_posix()}")

        is_windows = os.name == "nt"
        use_msvc_generator = is_windows and compiler is not None and compiler.family == "msvc"
        if use_msvc_generator:
            command.extend(["-G", "Visual Studio 17 2022", "-A", "x64"])
            vs = find_visual_studio()
            if vs is not None:
                command.append(f"-DCMAKE_GENERATOR_INSTANCE={vs.path.as_posix()}")
        elif compiler is not None and compiler.family != "msvc" and is_windows:
            if shutil.which("ninja"):
                command.extend(["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"])
            else:
                return Check(
                    "sdk-toolchain",
                    "error",
                    "non-MSVC toolchain selected on Windows requires Ninja; install Ninja or switch to msvc",
                    str(sdk.path),
                )
        else:
            command.append("-DCMAKE_BUILD_TYPE=Release")

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors="replace",
                timeout=120,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Check("sdk-toolchain", "error", f"deep probe failed: {exc}", str(sdk.path))
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            return Check("sdk-toolchain", "unsupported", f"configure failed: {failure_excerpt(output, 'configuration failed')}", str(sdk.path))

        build_command = ["cmake", "--build", str(build_dir), "--config", "Release"]
        try:
            build_result = subprocess.run(
                build_command,
                capture_output=True,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors="replace",
                timeout=180,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Check("sdk-toolchain", "error", f"deep build probe failed: {exc}", str(sdk.path))
        if build_result.returncode != 0:
            output = (build_result.stderr or build_result.stdout or "").strip()
            return Check("sdk-toolchain", "unsupported", f"build/link failed: {failure_excerpt(output, 'build failed')}", str(sdk.path))
        compiler_detail = f"with {compiler.family} {'.'.join(str(p) for p in compiler.version)}" if compiler else "with the current toolchain"
        return Check("sdk-toolchain", "ok", f"SDK application configured and linked {compiler_detail}", str(sdk.path))


def run_doctor(
    store: StateStore,
    project_root: Path | None = None,
    deep: bool = False,
    temp_root: Path | None = None,
) -> list[Check]:
    manifest = None
    if project_root:
        try:
            manifest = load_manifest(project_root)
        except ReleaseError:
            pass
    selected_compiler = (resolve_toolchain_compiler(manifest) if manifest else None) or resolve_compiler()
    checks: list[Check] = []
    with Spinner("Checking build tools"):
        checks.extend(_tool_versions(project_root))
    if os.name == "nt":
        with Spinner("Checking Visual Studio"):
            visual_studio = find_visual_studio()
            checks.extend(_visual_studio_check(visual_studio))
    else:
        visual_studio = None
    with Spinner("Checking compiler"):
        checks.extend(
            _compiler_check(
                temp_root=temp_root,
                visual_studio=visual_studio,
                project_root=project_root,
            )
        )
        checks.extend(_compiler_family_checks(selected_compiler))
    if project_root:
        with Spinner("Checking toolchain constraint"):
            checks.extend(_toolchain_constraint_checks(project_root))
    with Spinner("Checking system dependencies"):
        checks.extend(_linux_system_deps())
        checks.append(_opengl_check())

    window_backend, render_backend, fetch_mode = _detect_backends(project_root)
    checks.append(
        Check("window-backend", "ok", f"detected window backend: {window_backend}")
    )
    checks.append(
        Check("render-backend", "ok", f"detected render backend: {render_backend}")
    )

    if window_backend == "sdl2":
        checks.append(_sdl2_check(required=True))
    elif project_root is None:
        checks.append(_sdl2_check(required=False))

    if render_backend == "vulkan":
        checks.append(_vulkan_check(required=True))
    elif project_root is None:
        checks.append(_vulkan_check(required=False))

    network_required = fetch_mode
    try:
        if manifest:
            version = manifest.get("eui", {}).get("version")
            sdk = store.get_installed(version, normalize_platform(), normalize_arch()) if version else None
        else:
            sdk = store.active()
        if sdk and not has_configure_helper(sdk):
            cached_entry = store.sources_dir / sdk.version / "core/app/glfw_app_main.cpp"
            network_required = network_required or not cached_entry.is_file()
    except ReleaseError:
        pass
    if network_required:
        checks.append(_network_check(True))
    sdk_message = "Checking SDK and toolchain compatibility" if deep else "Checking SDK"
    with Spinner(sdk_message):
        checks.extend(_sdk_check(store, project_root, deep, temp_root=temp_root))
    return checks


# ---------------------------------------------------------------------------
# Dependency auto-fix support
# ---------------------------------------------------------------------------


def _detect_package_manager() -> str | None:
    """Detect a usable system package manager."""
    if os.name == "nt":
        for manager in ("winget", "choco"):
            if shutil.which(manager):
                return manager
        return None
    if _is_darwin():
        if shutil.which("brew"):
            return "brew"
        return None
    if _is_linux():
        for manager in ("apt", "dnf", "yum", "pacman", "zypper"):
            if shutil.which(manager):
                return manager
    return None


def _apt_packages(checks: list[Check], include_optional: bool = False) -> list[str]:
    """Collect apt packages for missing Linux dependency checks."""
    mapping = {
        "cmake": "cmake",
        "ninja": "ninja-build",
        "x11": "libx11-dev",
        "xrandr": "libxrandr-dev",
        "xinerama": "libxinerama-dev",
        "xcursor": "libxcursor-dev",
        "xi": "libxi-dev",
        "gl": "libgl1-mesa-dev",
        "libcurl": "libcurl4-openssl-dev",
        "pkg-config": "pkg-config",
        "sdl2": "libsdl2-dev",
        "vulkan": "libvulkan-dev",
        "tray-backend": "libappindicator3-dev libgtk-3-dev",
    }
    target_statuses = {"missing", "unsupported"}
    if include_optional:
        target_statuses.add("optional")
    packages: list[str] = []
    for check in checks:
        if check.status in target_statuses and check.name in mapping:
            packages.extend(mapping[check.name].split())
    return packages


def _install_command(check: Check, package_manager: str | None) -> tuple[list[str], str] | tuple[None, str]:
    """Return (command_list, description) to install/fix a single check, or (None, reason)."""
    if check.status not in {"missing", "unsupported", "optional"}:
        return None, "already satisfied"

    name = check.name

    # EUI-NEO SDK: install the exact pinned version via ENM itself.
    if name == "eui-sdk":
        match = re.search(r"EUI-NEO\s+(v[^\s(]+)", check.detail)
        version = match.group(1) if match else "latest"
        return [sys.executable, "-m", "enm", "sdk", "install", version], f"install EUI-NEO {version}"

    # Visual Studio on Windows: winget can install BuildTools but it usually
    # requires administrator privileges and a reboot; provide the command.
    if name in {"visual-studio", "vs-version"}:
        if package_manager == "winget":
            command = [
                "winget", "install", "Microsoft.VisualStudio.2022.BuildTools",
                "--override", "--wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended",
            ]
            return command, "install Visual Studio 2022 Build Tools with C++ workload (may require admin)"
        return None, "install Visual Studio 2022 Build Tools with C++ desktop workload from https://visualstudio.microsoft.com/downloads/"

    if os.name == "nt":
        windows_packages = {
            "cmake": (["winget", "install", "Kitware.CMake"], "CMake"),
            "ninja": (["winget", "install", "Ninja-build.Ninja"], "Ninja"),
            "xmake": (["winget", "install", "Xmake.Xmake"], "xmake"),
            "sdl2": (None, "download SDL2 from https://github.com/libsdl-org/SDL/releases and set SDL2 env var"),
            "vulkan": (None, "download Vulkan SDK from https://vulkan.lunarg.com/sdk/home"),
        }
        if name in windows_packages:
            cmd, desc = windows_packages[name]
            if cmd and package_manager == "winget":
                return cmd, f"install {desc}"
            return None, desc
        return None, "no automated installer available on Windows"

    if _is_darwin():
        if package_manager != "brew":
            return None, "install Homebrew from https://brew.sh to enable automated fixes"
        brew_packages = {
            "cmake": "cmake",
            "ninja": "ninja",
            "xmake": "xmake",
            "sdl2": "sdl2",
            "vulkan": "vulkan-sdk",
        }
        if name in brew_packages:
            return ["brew", "install", brew_packages[name]], f"install {brew_packages[name]}"
        return None, "no automated Homebrew formula for this dependency"

    if _is_linux():
        # apt is handled via grouped install; other package managers use per-check mapping.
        if package_manager == "apt":
            # Single-check fallback for things not grouped (e.g. cmake/ninja/xmake/sdl2/vulkan/pkg-config).
            single = {
                "cmake": "cmake",
                "ninja": "ninja-build",
                "xmake": "xmake",
                "pkg-config": "pkg-config",
                "sdl2": "libsdl2-dev",
                "vulkan": "libvulkan-dev",
            }
            if name in single:
                return ["sudo", "apt", "install", "-y", single[name]], f"install {single[name]}"
            return None, "will be grouped into a single apt install command"
        if package_manager in {"dnf", "yum"}:
            fedora_packages = {
                "cmake": "cmake",
                "ninja": "ninja-build",
                "xmake": "xmake",
                "x11": "libX11-devel",
                "xrandr": "libXrandr-devel",
                "xinerama": "libXinerama-devel",
                "xcursor": "libXcursor-devel",
                "xi": "libXi-devel",
                "gl": "mesa-libGL-devel",
                "libcurl": "libcurl-devel",
                "pkg-config": "pkgconfig",
                "sdl2": "SDL2-devel",
                "vulkan": "vulkan-loader-devel",
                "tray-backend": "libappindicator-gtk3-devel gtk3-devel",
            }
            if name in fedora_packages:
                return ["sudo", package_manager, "install", "-y", *fedora_packages[name].split()], f"install {fedora_packages[name]}"
        if package_manager == "pacman":
            arch_packages = {
                "cmake": "cmake",
                "ninja": "ninja",
                "xmake": "xmake",
                "x11": "libx11",
                "xrandr": "libxrandr",
                "xinerama": "libxinerama",
                "xcursor": "libxcursor",
                "xi": "libxi",
                "gl": "mesa",
                "libcurl": "curl",
                "pkg-config": "pkgconf",
                "sdl2": "sdl2",
                "vulkan": "vulkan-icd-loader",
                "tray-backend": "libappindicator-gtk3 gtk3",
            }
            if name in arch_packages:
                return ["sudo", "pacman", "-S", "--noconfirm", arch_packages[name]], f"install {arch_packages[name]}"
        return None, f"no automated install mapping for {package_manager}"

    return None, "unsupported platform"


def _run_install_command(command: list[str]) -> tuple[bool, str]:
    """Run an install command and return (success, detail)."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0:
        return True, output.splitlines()[0] if output else "installed"
    return False, output.splitlines()[-1] if output else f"exit code {result.returncode}"


def _prompt(question: str) -> bool:
    """Prompt the user for yes/no input."""
    try:
        answer = input(f"{question} [y/N]: ").strip().lower()
    except (EOFError, OSError):
        return False
    return answer in {"y", "yes"}


def _print_compiler_install_help(family: str, package_manager: str | None) -> None:
    """Print installation instructions for a missing compiler family."""
    if family == "msvc":
        print("MSVC must be installed manually. Download Visual Studio Build Tools from:")
        print("  https://visualstudio.microsoft.com/downloads/")
        return

    if family == "gcc":
        print("Install a matching GCC version, for example:")
        commands = {
            "apt": "sudo apt install g++",
            "brew": "brew install gcc",
            "winget": "winget install GnuWin32.GCC",
            "dnf": "sudo dnf install gcc-c++",
            "yum": "sudo yum install gcc-c++",
            "pacman": "sudo pacman -S gcc",
        }
    elif family == "clang":
        print("Install a matching Clang version, for example:")
        commands = {
            "apt": "sudo apt install clang",
            "brew": "brew install llvm",
            "winget": "winget install LLVM.LLVM",
            "dnf": "sudo dnf install clang",
            "yum": "sudo yum install clang",
            "pacman": "sudo pacman -S clang",
        }
    else:
        print(f"Install a matching {family} compiler using your system package manager.")
        return

    command = commands.get(package_manager)
    if command:
        print(f"  {command}")
    else:
        print(f"  install {family}++ using your system package manager")


def _toolchain_fix_guidance(check: Check, package_manager: str | None) -> None:
    """Print remediation guidance for a toolchain constraint mismatch."""
    detail = check.detail.lower()

    required_match = re.search(r"requires\s+(\w+)", detail)
    required = required_match.group(1) if required_match else None

    current_match = re.search(r"current\s+(?:compiler\s+)?is\s+(\w+)", detail)
    current = current_match.group(1) if current_match else None

    if required is None:
        print(f"Toolchain constraint not satisfied: {check.detail}")
        print("Alternatively, run 'enm lock-compiler' to update the manifest or 'enm configure --force' to ignore the constraint.")
        return

    if "no compiler was found" in detail:
        print(f"Toolchain constraint requires {required.upper()} but no matching compiler was found.")
        _print_compiler_install_help(required, package_manager)
    elif current and current != required:
        print(f"Project requires {required.upper()} but the active compiler is {current.upper()}.")
        print(f"Run 'enm lock-compiler' to switch this project to {current.upper()}, or run 'enm configure --force' to ignore the constraint.")
        return
    else:
        print(f"Toolchain constraint not satisfied: {check.detail}")
        _print_compiler_install_help(required, package_manager)

    print("Alternatively, run 'enm lock-compiler' to update the manifest or 'enm configure --force' to ignore the constraint.")


def fix_missing_dependencies(checks: list[Check], *, yes: bool = False, force: bool = False) -> list[Check]:
    """Interactively offer to install missing/unsupported dependencies.

    Before installing, the missing items are listed and the user is asked once.
    Required items are installed automatically when ``yes=True``.
    Optional items are only offered when ``force=True``; they are always prompted
    and never auto-installed, even when ``yes=True``.
    Returns a new list of checks re-run for any items that were fixed.
    """
    package_manager = _detect_package_manager()
    approved_apt: list[tuple[int, Check, list[str], str]] = []

    # Toolchain constraints need guidance rather than automated installation.
    for check in checks:
        if check.name == "toolchain" and check.status in {"missing", "unsupported"}:
            _toolchain_fix_guidance(check, package_manager)

    def _prompt_install_check(check: Check, index: int, auto: bool) -> None:
        command, description = _install_command(check, package_manager)
        if command is None:
            print(f"  Cannot auto-fix {check.name}: {description}")
            return
        if not auto:
            if not _prompt(f"Install/fix {check.name} ({description})?"):
                return
        # Batch apt packages to avoid repeated sudo prompts; install others immediately.
        if package_manager == "apt" and len(command) >= 4 and command[:4] == ["sudo", "apt", "install", "-y"]:
            approved_apt.append((index, check, command[4:], description))
            return
        with Spinner(f"Installing {check.name}: {description}"):
            success, detail = _run_install_command(command)
        if success:
            print(f"  {GREEN}ok{RESET}: {check.name} {detail}" if sys.stderr.isatty() else f"  ok: {check.name} {detail}")
            checks[index] = Check(check.name, "fixed", f"installed: {description}", check.path, check.required)
        else:
            print(f"  {RED}failed{RESET}: {check.name} {detail}" if sys.stderr.isatty() else f"  failed: {check.name} {detail}")

    # 1) Required items: auto-install with --yes, otherwise prompt one by one.
    for index, check in enumerate(checks):
        if check.status in {"missing", "unsupported"} and check.required:
            _prompt_install_check(check, index, auto=yes)

    # 2) Optional items: only offered when force=True; always prompt one by one.
    if force:
        for index, check in enumerate(checks):
            if check.status == "optional" and not check.required:
                _prompt_install_check(check, index, auto=False)

    # 3) Install all approved apt packages in one command.
    if approved_apt:
        packages: list[str] = []
        for _index, _check, pkgs, _description in approved_apt:
            packages.extend(pkgs)
        packages = list(dict.fromkeys(packages))
        subprocess.run(["sudo", "apt", "update"], check=False, timeout=120)
        message = f"Installing approved packages via apt: {' '.join(packages)}"
        with Spinner(message):
            success, detail = _run_install_command(["sudo", "apt", "install", "-y", *packages])
        if success:
            print(f"  {GREEN}ok{RESET}: {message} {detail}" if sys.stderr.isatty() else f"  ok: {message} {detail}")
            for index, _check, _pkgs, description in approved_apt:
                checks[index] = Check(_check.name, "fixed", f"installed: {description}", _check.path, _check.required)
        else:
            print(f"  {RED}failed{RESET}: {message} {detail}" if sys.stderr.isatty() else f"  failed: {message} {detail}")

    return checks


def checks_as_dict(checks: list[Check]) -> list[dict[str, object]]:
    return [asdict(check) for check in checks]
