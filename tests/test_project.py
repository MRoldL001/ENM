from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from enm.package import package_stage
from enm.project import (
    _write_cmake_initial_cache,
    create_project,
    generate_ci,
    load_manifest,
    supports_external_apps,
)
from enm.state import InstalledSdk


class ProjectTests(unittest.TestCase):
    def test_project_is_pinned_to_release_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample"
            create_project(root, "Sample App", "v9.8.7")
            manifest = load_manifest(root)
            self.assertEqual(manifest["version"], "0.1.0")
            self.assertEqual(manifest["eui"]["version"], "v9.8.7")
            cmake = (root / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn("find_package(EuiNeo ${ENM_EUI_VERSION} EXACT CONFIG REQUIRED)", cmake)
            self.assertIn("add_executable(${ENM_TARGET} src/app.cpp)", cmake)
            self.assertNotIn("v9.8.7", cmake)
            self.assertNotIn("Sample_App", cmake)
            self.assertIn("EUI_NEO_LEGACY_SOURCE", cmake)
            self.assertIn("core/app/glfw_app_main.cpp", cmake)
            self.assertIn("set(CMAKE_CXX_STANDARD 17)", cmake)
            self.assertIn("add_test(NAME ${ENM_TARGET}.app_config COMMAND ${ENM_TARGET}_tests)", cmake)
            self.assertIn("add_executable(${ENM_TARGET}_tests EXCLUDE_FROM_ALL", cmake)
            self.assertNotIn("tests/app_config_test.cpp src/app.cpp", cmake)
            self.assertTrue((root / "src/app.cpp").is_file())
            self.assertTrue((root / "tests/app_config_test.cpp").is_file())
            self.assertFalse((root / "CMakePresets.json").exists())
            self.assertFalse((root / ".github/workflows/build.yml").exists())

    def test_cmake_cache_is_derived_from_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample"
            create_project(root, "Sample App", "v9.8.7")
            cache = _write_cmake_initial_cache(root, load_manifest(root)).read_text(encoding="utf-8")
            self.assertIn('set(ENM_TARGET "Sample_App"', cache)
            self.assertIn('set(ENM_PROJECT_VERSION "0.1.0"', cache)
            self.assertIn('set(ENM_EUI_VERSION "9.8.7"', cache)
            self.assertEqual(cache.count(" FORCE)"), 3)

    def test_ci_is_explicit_and_quotes_install_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample"
            create_project(root, "Sample App", "v9.8.7")
            workflow = generate_ci(
                root,
                "v9.8.7",
                "git+https://github.com/example/enm.git@v0.1.0",
            ).read_text(encoding="utf-8")
            self.assertIn('ENM_INSTALL_SPEC: "git+https://github.com/example/enm.git@v0.1.0"', workflow)
            self.assertIn('pip install "$env:ENM_INSTALL_SPEC"', workflow)
            self.assertIn("enm test", workflow)

    def test_external_app_support_is_detected_from_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = InstalledSdk("v1.0.0", "windows", "x64", root, "sdk.zip", "abc")
            self.assertFalse(supports_external_apps(sdk))
            config = root / "lib/cmake/EuiNeo/EuiNeoConfig.cmake"
            config.parent.mkdir(parents=True)
            config.write_text("function(eui_neo_configure_app target)\nendfunction()\n", encoding="utf-8")
            self.assertTrue(supports_external_apps(sdk))

    def test_library_only_sdk_uses_legacy_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = InstalledSdk("v0.5.5", "windows", "x64", root, "sdk.zip", "abc")
            targets = root / "lib/cmake/EuiNeo/EuiNeoTargets.cmake"
            targets.parent.mkdir(parents=True)
            targets.write_text("add_library(eui::neo STATIC IMPORTED)\n", encoding="utf-8")
            self.assertTrue(supports_external_apps(sdk))

    def test_package_writes_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "dist/app-v1-windows-x64"
            stage.mkdir(parents=True)
            (stage / "app.exe").write_bytes(b"app")
            archive, digest = package_stage(stage, "zip")
            self.assertTrue(archive.exists())
            self.assertIn(archive.name, digest.read_text(encoding="ascii"))


if __name__ == "__main__":
    unittest.main()
