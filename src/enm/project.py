from __future__ import annotations

import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .github import Asset, ReleaseError, download_asset, extract_archive, normalize_arch, normalize_platform
from .msvc import check_sdk_abi
from .state import InstalledSdk, StateStore
from .ui import Spinner


MANIFEST = "enm-project.json"


def _safe_name(name: str) -> str:
    target = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip().replace("-", "_"))
    if not target or not re.match(r"^[A-Za-z_]", target):
        target = f"app_{target}"
    return target


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_project(destination: Path, name: str, version: str, force: bool = False) -> Path:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()) and not force:
        raise ReleaseError(f"destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    target = _safe_name(name)
    numeric_version = version.removeprefix("v")
    manifest = {
        "schema": 1,
        "name": name,
        "version": "0.1.0",
        "target": target,
        "eui": {"version": version},
        "build_dir": "build/default",
    }
    _write(destination / MANIFEST, json.dumps(manifest, indent=2) + "\n")
    _write(
        destination / "CMakeLists.txt",
        f'''cmake_minimum_required(VERSION 3.14)\nproject({target} VERSION 0.1.0 LANGUAGES C CXX)\n\nset(CMAKE_CXX_STANDARD 17)\nset(CMAKE_CXX_STANDARD_REQUIRED ON)\n\nfind_package(EuiNeo {numeric_version} EXACT CONFIG REQUIRED)\nadd_executable({target} src/app.cpp)\n\nif(COMMAND eui_neo_configure_app)\n    eui_neo_configure_app({target})\nelse()\n    set(_enm_legacy_source "$ENV{{EUI_NEO_LEGACY_SOURCE}}")\n    if(NOT EXISTS "${{_enm_legacy_source}}/core/app/glfw_app_main.cpp")\n        message(FATAL_ERROR "ENM legacy compatibility source is missing; run enm configure again while online.")\n    endif()\n    get_target_property(_enm_eui_definitions eui::neo INTERFACE_COMPILE_DEFINITIONS)\n    set(_enm_app_main "${{_enm_legacy_source}}/core/app/glfw_app_main.cpp")\n    if("${{_enm_eui_definitions}}" MATCHES "EUI_WINDOW_BACKEND_SDL2")\n        set(_enm_app_main "${{_enm_legacy_source}}/core/app/sdl2_app_main.cpp")\n    endif()\n    target_sources({target} PRIVATE "${{_enm_app_main}}")\n    target_include_directories({target} PRIVATE "${{_enm_legacy_source}}")\n    target_link_libraries({target} PRIVATE eui::neo)\n    if(MSVC)\n        target_compile_options({target} PRIVATE /O1 /GS- /sdl- /utf-8 /wd4819)\n        target_link_options({target} PRIVATE /OPT:REF /OPT:ICF /ENTRY:mainCRTStartup /SUBSYSTEM:WINDOWS)\n        set_target_properties({target} PROPERTIES VS_GLOBAL_VcpkgXUseBuiltInApplocalDeps true)\n    elseif(APPLE)\n        target_compile_options({target} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-Os -fno-exceptions -fno-rtti>)\n        target_link_options({target} PRIVATE -Wl,-dead_strip)\n    else()\n        target_compile_options({target} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-Os -fno-exceptions -fno-rtti>)\n        target_link_options({target} PRIVATE -Wl,--gc-sections -s)\n    endif()\n    if(EXISTS "${{_enm_legacy_source}}/assets")\n        add_custom_command(TARGET {target} POST_BUILD\n            COMMAND ${{CMAKE_COMMAND}} -E make_directory "$<TARGET_FILE_DIR:{target}>/assets"\n            COMMAND ${{CMAKE_COMMAND}} -E copy_directory "${{_enm_legacy_source}}/assets" "$<TARGET_FILE_DIR:{target}>/assets")\n    endif()\nendif()\n\ninclude(CTest)\nif(BUILD_TESTING)\n    add_executable({target}_tests EXCLUDE_FROM_ALL tests/app_config_test.cpp src/app.cpp)\n    target_link_libraries({target}_tests PRIVATE eui::neo)\n    if(MSVC)\n        target_compile_options({target}_tests PRIVATE /utf-8)\n    endif()\n    add_test(NAME {target}.app_config COMMAND {target}_tests)\nendif()\n''',
    )
    _write(
        destination / "src/app.cpp",
        f'''#include "eui_neo.h"\n\nnamespace app {{\n\nconst DslAppConfig& dslAppConfig() {{\n    static const DslAppConfig config = DslAppConfig{{}}\n        .title("{name}")\n        .pageId("{target}")\n        .windowSize(960, 640);\n    return config;\n}}\n\nvoid compose(eui::Ui& ui, const eui::Screen& screen) {{\n    ui.column("root")\n        .size(screen.width, screen.height)\n        .padding(32.0f)\n        .content([&] {{\n            ui.text("title")\n                .text("Hello from {name}")\n                .fontSize(28.0f)\n                .build();\n        }})\n        .build();\n}}\n\n}} // namespace app\n''',
    )
    _write(
        destination / "tests/app_config_test.cpp",
        f'''#include "eui_neo.h"\n\n#include <iostream>\n#include <string>\n\nint main() {{\n    const auto& config = app::dslAppConfig();\n    if (std::string(config.titleValue) != "{name}" ||\n        std::string(config.pageIdValue) != "{target}" ||\n        config.windowWidthValue != 960 ||\n        config.windowHeightValue != 640) {{\n        std::cerr << "unexpected application configuration\\n";\n        return 1;\n    }}\n    return 0;\n}}\n''',
    )
    cmake_path = destination / "CMakeLists.txt"
    cmake_content = cmake_path.read_text(encoding="utf-8")
    cmake_content = cmake_content.replace(
        f"project({target} VERSION 0.1.0 LANGUAGES C CXX)",
        '''if(NOT DEFINED ENM_TARGET OR NOT DEFINED ENM_PROJECT_VERSION OR NOT DEFINED ENM_EUI_VERSION)
    message(FATAL_ERROR "Missing ENM project configuration. Run 'enm configure' instead of invoking CMake directly.")
endif()
project(${ENM_TARGET} VERSION ${ENM_PROJECT_VERSION} LANGUAGES C CXX)''',
    )
    cmake_content = cmake_content.replace(
        f"find_package(EuiNeo {numeric_version} EXACT CONFIG REQUIRED)",
        "find_package(EuiNeo ${ENM_EUI_VERSION} EXACT CONFIG REQUIRED)",
    )
    cmake_content = cmake_content.replace(target, "${ENM_TARGET}")
    _write(
        cmake_path,
        cmake_content.replace(
            "tests/app_config_test.cpp src/app.cpp", "tests/app_config_test.cpp"
        ),
    )
    test_path = destination / "tests/app_config_test.cpp"
    _write(
        test_path,
        test_path.read_text(encoding="utf-8").replace(
            '#include "eui_neo.h"', '#include "../src/app.cpp"'
        ),
    )
    _write(destination / ".gitignore", "/build/\n/dist/\n")
    return destination


def find_project(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / MANIFEST).is_file():
            return candidate
    raise ReleaseError(f"{MANIFEST} was not found from {current}")


def load_manifest(root: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid {MANIFEST}: {exc}") from exc
    if manifest.get("schema") != 1 or not manifest.get("target") or not manifest.get("version"):
        raise ReleaseError(f"unsupported or incomplete {MANIFEST}")
    return manifest


def _write_cmake_initial_cache(root: Path, manifest: dict[str, Any]) -> Path:
    cache = root / manifest.get("build_dir", "build/default") / "enm-config.cmake"
    values = {
        "ENM_TARGET": str(manifest["target"]),
        "ENM_PROJECT_VERSION": str(manifest["version"]),
        "ENM_EUI_VERSION": str(manifest["eui"]["version"]).removeprefix("v"),
    }
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
            raise ReleaseError(f"unsafe {key} value in {MANIFEST}: {value}")
    _write(cache, "".join(
        f'set({key} "{value}" CACHE STRING "Generated by ENM" FORCE)\n'
        for key, value in values.items()
    ))
    return cache


def has_configure_helper(sdk: InstalledSdk) -> bool:
    for cmake_file in sdk.path.rglob("*.cmake"):
        try:
            content = cmake_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"(?:function|macro)\s*\(\s*eui_neo_configure_app\b", content, re.IGNORECASE):
            return True
    return False


def supports_external_apps(sdk: InstalledSdk) -> bool:
    if has_configure_helper(sdk):
        return True
    for cmake_file in sdk.path.rglob("EuiNeoTargets.cmake"):
        try:
            content = cmake_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "eui::neo" in content:
            return True
    return False


def _legacy_source(store: StateStore, version: str) -> Path:
    destination = store.sources_dir / version
    marker = destination / "core/app/glfw_app_main.cpp"
    if marker.is_file():
        return destination
    store.ensure()
    archive = store.tmp_dir / f"eui-neo-{version}.zip"
    staging = store.tmp_dir / f"eui-neo-source-{uuid.uuid4().hex}"
    asset = Asset(
        name=f"EUI-NEO-{version}-source.zip",
        url=f"https://github.com/sudoevolve/EUI-NEO/archive/refs/tags/{version}.zip",
        size=0,
        digest=None,
    )
    try:
        download_asset(asset, archive)
        extract_archive(archive, staging)
        roots = [path for path in staging.iterdir() if path.is_dir()]
        if len(roots) != 1 or not (roots[0] / "core/app/glfw_app_main.cpp").is_file():
            raise ReleaseError(f"EUI-NEO {version} source does not contain a compatible application entry")
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        roots[0].replace(destination)
    finally:
        archive.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def project_sdk(root: Path, store: StateStore) -> InstalledSdk:
    manifest = load_manifest(root)
    version = manifest.get("eui", {}).get("version")
    if not version:
        raise ReleaseError(f"{MANIFEST} does not pin an EUI-NEO release")
    sdk = store.get_installed(version, normalize_platform(), normalize_arch())
    if not supports_external_apps(sdk):
        raise ReleaseError(
            f"EUI-NEO SDK {version} exports neither eui_neo_configure_app() nor eui::neo; "
            "install and select a compatible release SDK"
        )
    return sdk


def _run(
    command: list[str], root: Path, sdk: InstalledSdk, extra_environment: dict[str, str] | None = None
) -> int:
    environment = os.environ.copy()
    environment["EUI_NEO_SDK_ROOT"] = str(sdk.path)
    environment.update(extra_environment or {})
    with Spinner(f"Running {Path(command[0]).name}"):
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    if result.stdout:
        stream = getattr(sys.stdout, "buffer", None)
        if stream is not None:
            stream.write(result.stdout)
            stream.flush()
        else:
            print(result.stdout.decode(locale.getpreferredencoding(False), errors="replace"), end="")
    output = (result.stdout or b"").decode(locale.getpreferredencoding(False), errors="replace")
    if result.returncode and any(
        symbol in output
        for symbol in ("__std_search_1", "__std_find_end_1", "__std_find_first_of_trivial_pos_1")
    ):
        print(
            "\nENM diagnosis: the published SDK was built with a newer MSVC STL "
            "than the local linker provides. Update Visual Studio 2022 Build Tools, then "
            "reconfigure from a clean build directory.",
            file=sys.stderr,
        )
    return result.returncode


def configure(root: Path, store: StateStore, extra: list[str] | None = None) -> int:
    manifest = load_manifest(root)
    sdk = project_sdk(root, store)
    extra_environment: dict[str, str] = {}
    if not has_configure_helper(sdk):
        with Spinner(f"Preparing EUI-NEO {sdk.version} compatibility source"):
            extra_environment["EUI_NEO_LEGACY_SOURCE"] = str(_legacy_source(store, sdk.version))
    if os.name == "nt":
        compatibility = check_sdk_abi(sdk)
        if compatibility.status == "unsupported":
            raise ReleaseError(compatibility.detail)
    build_dir = manifest.get("build_dir", "build/default")
    initial_cache = _write_cmake_initial_cache(root, manifest)
    command = [
        "cmake",
        "-C",
        str(initial_cache),
        "-S",
        ".",
        "-B",
        build_dir,
        f"-DCMAKE_PREFIX_PATH={sdk.path}",
    ]
    if platform.system().lower() == "windows":
        command.extend(["-G", "Visual Studio 17 2022", "-A", "x64"])
    elif shutil.which("ninja"):
        command.extend(["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"])
    else:
        command.extend(["-G", "Unix Makefiles", "-DCMAKE_BUILD_TYPE=Release"])
    return _run([*command, *(extra or [])], root, sdk, extra_environment)


def build(root: Path, store: StateStore, extra: list[str] | None = None) -> int:
    manifest = load_manifest(root)
    sdk = project_sdk(root, store)
    return _run(
        ["cmake", "--build", manifest.get("build_dir", "build/default"), "--config", "Release", *(extra or [])],
        root,
        sdk,
    )


def test(root: Path, store: StateStore, extra: list[str] | None = None) -> int:
    manifest = load_manifest(root)
    sdk = project_sdk(root, store)
    build_dir = manifest.get("build_dir", "build/default")
    result = _run(
        [
            "cmake",
            "--build",
            build_dir,
            "--config",
            "Release",
            "--target",
            f"{manifest['target']}_tests",
        ],
        root,
        sdk,
    )
    if result:
        return result
    return _run(
        ["ctest", "--test-dir", build_dir, "-C", "Release", "--output-on-failure", *(extra or [])],
        root,
        sdk,
    )


def generate_ci(root: Path, version: str, install_spec: str) -> Path:
    if not install_spec.strip():
        raise ReleaseError("a non-empty ENM install specification is required")
    current_platform = normalize_platform()
    runner = {"windows": "windows-2022", "linux": "ubuntu-latest", "macos": "macos-latest"}[
        current_platform
    ]
    install_argument = "$env:ENM_INSTALL_SPEC" if current_platform == "windows" else "$ENM_INSTALL_SPEC"
    workflow = f'''name: build\n\non:\n  push:\n  pull_request:\n  workflow_dispatch:\n\npermissions:\n  contents: read\n\nenv:\n  ENM_INSTALL_SPEC: {json.dumps(install_spec)}\n\njobs:\n  build:\n    runs-on: {runner}\n    steps:\n      - uses: actions/checkout@v5\n      - uses: actions/setup-python@v6\n        with:\n          python-version: "3.13"\n      - name: Install ENM\n        run: python -m pip install "{install_argument}"\n      - name: Install pinned EUI-NEO SDK\n        run: enm sdk install {version}\n      - name: Configure\n        run: enm configure\n      - name: Build\n        run: enm build\n      - name: Test\n        run: enm test\n      - name: Package\n        run: enm package --format zip\n      - uses: actions/upload-artifact@v6\n        with:\n          name: application-package\n          path: dist/*.zip*\n          if-no-files-found: error\n'''
    path = root / ".github/workflows/build.yml"
    _write(path, workflow)
    return path
