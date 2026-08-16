from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .github import ReleaseError
from .msvc import find_visual_studio
from .project import load_manifest
from .state import StateStore
from .ui import Spinner


@dataclass
class Check:
    name: str
    status: str
    detail: str
    path: str | None = None
    required: bool = True


# EUI-NEO upstream requirements (from README and CMakeLists.txt)
CMAKE_MINIMUM = (3, 14)
MSVC_MINIMUM = (19, 29)  # Visual Studio 2019 16.11
GCC_MINIMUM = (12, 0)
CLANG_MINIMUM = (14, 0)
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


def _tool_versions() -> tuple[Check, ...]:
    return (
        _probe("cmake", ["--version"], CMAKE_MINIMUM),
        _probe("ctest", ["--version"], CMAKE_MINIMUM),
        _probe("ninja", ["--version"], missing_status="optional"),
        _probe("xmake", ["--version"], missing_status="optional"),
    )


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


def _compiler_check(temp_root: Path | None = None, visual_studio=None) -> list[Check]:
    # On Windows ENM uses the Visual Studio generator, so check MSVC even when
    # another compiler (e.g. MinGW) happens to be first on PATH.
    path: Path | None = None
    family: str | None = None
    if os.name == "nt" and visual_studio:
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
    if version and minimum:
        if version[: len(minimum)] < minimum:
            checks.append(
                Check(
                    "compiler",
                    "unsupported",
                    f"{detail}; EUI-NEO requires {family} >= {_minimum_label(minimum)}",
                    str(path),
                )
            )
        else:
            checks.append(Check("compiler", "ok", detail, str(path)))
    else:
        checks.append(Check("compiler", "ok", detail, str(path)))
    checks.append(_cpp17_probe(path, family, temp_root=temp_root, visual_studio=visual_studio))
    return checks


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
    # A lightweight check: can we reach GitHub?
    try:
        request = subprocess.run(
            ["python", "-c", "import urllib.request; urllib.request.urlopen('https://github.com', timeout=5)"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if request.returncode == 0:
            return Check("network", "ok", "can reach GitHub")
    except (OSError, subprocess.TimeoutExpired):
        pass
    status = "missing" if fetch_mode else "optional"
    return Check("network", status, "cannot reach github.com; required when EUI_DEPS_MODE=fetch or legacy source download is needed", required=fetch_mode)


def _detect_backends(project_root: Path | None) -> tuple[str, str, bool]:
    """Return (window_backend, render_backend, fetch_mode) defaults from manifest or build dir."""
    window_backend = "glfw"
    render_backend = "opengl"
    fetch_mode = False
    if project_root:
        try:
            manifest = load_manifest(project_root)
            # Manifest does not yet store backend preferences; infer from build directory name if present
            build_dir_name = Path(manifest.get("build_dir", "build/default")).name.lower()
        except ReleaseError:
            build_dir_name = "default"
        if "sdl2" in build_dir_name:
            window_backend = "sdl2"
        if "vk" in build_dir_name or "vulkan" in build_dir_name:
            render_backend = "vulkan"
    return window_backend, render_backend, fetch_mode


def _sdk_check(store: StateStore, project_root: Path | None, deep: bool, temp_root: Path | None = None) -> list[Check]:
    checks: list[Check] = []
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
    has_configure_helper = False
    has_eui_neo_target = False
    for cmake_file in sdk.path.rglob("*.cmake"):
        try:
            content = cmake_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"(?:function|macro)\s*\(\s*eui_neo_configure_app\b", content, re.IGNORECASE):
            has_configure_helper = True
        if "eui::neo" in content:
            has_eui_neo_target = True
    if has_configure_helper:
        detail_parts.append("eui_neo_configure_app() available")
    elif has_eui_neo_target:
        detail_parts.append("eui::neo target available")
    else:
        detail_parts.append("no supported entry point")

    status = "ok" if (header and library and (has_configure_helper or has_eui_neo_target)) else "error"
    checks.append(Check("eui-sdk", status, "; ".join(detail_parts), str(sdk.path)))

    if deep and status == "ok":
        checks.append(_deep_sdk_probe(sdk, config, temp_root=temp_root))

    return checks


def _deep_sdk_probe(sdk, config: Path, temp_root: Path | None = None) -> Check:
    """Configure a minimal CMake project against the SDK to verify real compatibility."""
    with tempfile.TemporaryDirectory(prefix="enm-doctor-sdk-", dir=temp_root) as directory:
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
            "    target_link_libraries(probe PRIVATE eui::neo)\n"
            "endif()\n",
            encoding="utf-8",
        )
        build_dir = root / "build"
        command = ["cmake", "-S", str(root), "-B", str(build_dir), f"-DCMAKE_PREFIX_PATH={sdk.path.as_posix()}"]
        if os.name == "nt":
            command.extend(["-G", "Visual Studio 17 2022", "-A", "x64"])
        else:
            command.append("-DCMAKE_BUILD_TYPE=Release")
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Check("sdk-toolchain", "error", f"deep probe failed: {exc}", str(sdk.path))
        if result.returncode == 0:
            return Check("sdk-toolchain", "ok", "SDK can be configured with the active toolchain", str(sdk.path))
        output = (result.stderr or result.stdout or "").strip()
        first_error = next((line for line in output.splitlines() if "error" in line.lower() or "fatal" in line.lower()), output.splitlines()[0] if output else "configuration failed")
        return Check("sdk-toolchain", "unsupported", first_error, str(sdk.path))


def run_doctor(
    store: StateStore,
    project_root: Path | None = None,
    deep: bool = False,
    temp_root: Path | None = None,
) -> list[Check]:
    # Use a project-local temp directory when a project is provided so that
    # ephemeral doctor probes do not scatter files outside the workspace.
    if project_root and not temp_root:
        temp_root = project_root / ".enm-doctor-tmp"
        temp_root.mkdir(exist_ok=True)
    checks: list[Check] = []
    with Spinner("Checking build tools"):
        checks.extend(_tool_versions())
    if os.name == "nt":
        with Spinner("Checking Visual Studio"):
            visual_studio = find_visual_studio()
            checks.extend(_visual_studio_check(visual_studio))
    else:
        visual_studio = None
    with Spinner("Checking compiler"):
        checks.extend(_compiler_check(temp_root=temp_root, visual_studio=visual_studio))
    with Spinner("Checking system dependencies"):
        checks.extend(_linux_system_deps())
        checks.append(_opengl_check())

    window_backend, render_backend, fetch_mode = _detect_backends(project_root)
    checks.append(
        Check("window-backend", "ok", f"detected window backend: {window_backend}", required=False)
    )
    checks.append(
        Check("render-backend", "ok", f"detected render backend: {render_backend}", required=False)
    )

    if window_backend == "sdl2":
        checks.append(_sdl2_check(required=True))
    else:
        # GLFW is bundled in the SDK source, but for source builds it may need fetching
        checks.append(_sdl2_check(required=False))

    if render_backend == "vulkan":
        checks.append(_vulkan_check(required=True))
    else:
        checks.append(_vulkan_check(required=False))

    checks.append(_network_check(fetch_mode))
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

    # EUI-NEO SDK: handled separately via ENM itself.
    if name == "eui-sdk":
        return None, "run 'enm sdk install latest' to install the SDK"

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
        print(f"  Installing {check.name}: {description}")
        success, detail = _run_install_command(command)
        if success:
            print(f"    ok: {detail}")
            checks[index] = Check(check.name, "fixed", f"installed: {description}", check.path, check.required)
        else:
            print(f"    failed: {detail}")

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
        print(f"\nInstalling approved packages via apt: {' '.join(packages)}")
        subprocess.run(["sudo", "apt", "update"], check=False, timeout=120)
        success, detail = _run_install_command(["sudo", "apt", "install", "-y", *packages])
        if success:
            print(f"  ok: {detail}")
            for index, _check, _pkgs, description in approved_apt:
                checks[index] = Check(_check.name, "fixed", f"installed: {description}", _check.path, _check.required)
        else:
            print(f"  failed: {detail}")

    return checks


def checks_as_dict(checks: list[Check]) -> list[dict[str, object]]:
    return [asdict(check) for check in checks]
