from __future__ import annotations

import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .github import ReleaseError, normalize_arch, normalize_platform
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
        "target": target,
        "eui": {"version": version},
        "build_dir": "build/default",
    }
    _write(destination / MANIFEST, json.dumps(manifest, indent=2) + "\n")
    _write(
        destination / "CMakeLists.txt",
        f'''cmake_minimum_required(VERSION 3.14)\nproject({target} VERSION 0.1.0 LANGUAGES CXX)\n\nfind_package(EuiNeo {numeric_version} EXACT CONFIG REQUIRED)\n\nadd_executable({target} src/app.cpp)\neui_neo_configure_app({target})\n\nenable_testing()\nadd_test(NAME toolchain_smoke COMMAND ${{CMAKE_COMMAND}} -E echo "{target} built successfully")\n''',
    )
    _write(
        destination / "src/app.cpp",
        f'''#include "eui_neo.h"\n\nnamespace app {{\n\nconst DslAppConfig& dslAppConfig() {{\n    static const DslAppConfig config = DslAppConfig{{}}\n        .title("{name}")\n        .pageId("{target}")\n        .windowSize(960, 640);\n    return config;\n}}\n\nvoid compose(eui::Ui& ui, const eui::Screen& screen) {{\n    ui.column("root")\n        .size(screen.width, screen.height)\n        .padding(32.0f)\n        .content([&] {{\n            ui.text("title")\n                .text("Hello from {name}")\n                .fontSize(28.0f)\n                .build();\n        }})\n        .build();\n}}\n\n}} // namespace app\n''',
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
    if manifest.get("schema") != 1 or not manifest.get("target"):
        raise ReleaseError(f"unsupported or incomplete {MANIFEST}")
    return manifest


def project_sdk(root: Path, store: StateStore) -> InstalledSdk:
    manifest = load_manifest(root)
    version = manifest.get("eui", {}).get("version")
    if not version:
        raise ReleaseError(f"{MANIFEST} does not pin an EUI-NEO release")
    return store.get_installed(version, normalize_platform(), normalize_arch())


def _run(command: list[str], root: Path, sdk: InstalledSdk) -> int:
    environment = os.environ.copy()
    environment["EUI_NEO_SDK_ROOT"] = str(sdk.path)
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
    if os.name == "nt":
        compatibility = check_sdk_abi(sdk)
        if compatibility.status == "unsupported":
            raise ReleaseError(compatibility.detail)
    build_dir = manifest.get("build_dir", "build/default")
    command = [
        "cmake",
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
    return _run([*command, *(extra or [])], root, sdk)


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
    return _run(
        ["ctest", "--test-dir", manifest.get("build_dir", "build/default"), "-C", "Release", "--output-on-failure", *(extra or [])],
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
