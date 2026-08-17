from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

UnameResult = namedtuple("UnameResult", ["sysname", "nodename", "release", "version", "machine"])

from enm.doctor import (
    Check,
    _apt_packages,
    _compiler_family,
    _compiler_family_checks,
    _compiler_version,
    _cpp17_probe,
    _detect_backends,
    _detect_package_manager,
    _deep_sdk_probe,
    _find_compiler,
    _install_command,
    _linux_system_deps,
    _opengl_check,
    _pkg_config_exists,
    _pkg_config_version,
    _probe,
    _run_install_command,
    _sdl2_check,
    _sdk_check,
    _tool_versions,
    _toolchain_constraint_checks,
    _vulkan_check,
    fix_missing_dependencies,
    run_doctor,
)
from enm.state import InstalledSdk, StateStore
from enm.msvc import AbiResult
from enm.toolchain import Compiler


class DoctorProbeTests(unittest.TestCase):
    def test_probe_flags_unsupported_version(self):
        def fake_run(cmd, **kwargs):
            self.assertIn("cmake", cmd[0].lower())
            return mock.MagicMock(returncode=0, stdout="cmake version 3.10.0\n", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            check = _probe("cmake", ["--version"], (3, 14))
        self.assertEqual(check.status, "unsupported")
        self.assertIn("3.14", check.detail)

    def test_probe_missing_tool(self):
        with mock.patch("shutil.which", return_value=None):
            check = _probe("ninja", ["--version"], missing_status="optional")
        self.assertEqual(check.status, "optional")

    def test_compiler_family_detection(self):
        self.assertEqual(_compiler_family("cl.exe"), "msvc")
        self.assertEqual(_compiler_family("g++"), "gcc")
        self.assertEqual(_compiler_family("clang++"), "clang")
        self.assertEqual(_compiler_family("clang-cl"), "clang")

    def test_find_compiler_prefers_cxx_env(self):
        fake_path = Path("/fake/g++")
        with mock.patch.dict(os.environ, {"CXX": str(fake_path)}):
            with mock.patch("shutil.which", return_value=str(fake_path)):
                path, family = _find_compiler()
        self.assertEqual(path, fake_path)
        self.assertEqual(family, "gcc")

    def test_cpp17_probe_success(self):
        def fake_run(cmd, **kwargs):
            output_path = Path(cmd[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("")
            return mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            check = _cpp17_probe(Path("/fake/g++"), "gcc")
        self.assertEqual(check.status, "ok")

    def test_cpp17_probe_failure(self):
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="", stderr="missing header")):
            check = _cpp17_probe(Path("/fake/g++"), "gcc")
        self.assertEqual(check.status, "unsupported")


class DoctorCompilerFamilyTests(unittest.TestCase):
    def test_family_checks_return_parent_and_children(self):
        from enm.toolchain import Compiler

        compilers = [
            Compiler(Path("/usr/bin/cl"), "msvc", (19, 44), "cl"),
            Compiler(Path("/usr/bin/g++"), "gcc", (13, 2), "g++"),
        ]
        with mock.patch("enm.doctor.detect_compilers", return_value=compilers):
            checks = _compiler_family_checks()
        names = {c.name: c for c in checks}
        self.assertIn("compiler", names)
        self.assertEqual(names["compiler"].status, "ok")
        self.assertEqual(names["msvc"].status, "ok")
        self.assertEqual(names["gcc"].status, "ok")
        self.assertEqual(names["clang"].status, "optional")
        self.assertEqual(names["msvc"].parent, "compiler")

    def test_family_checks_report_unsupported_version(self):
        from enm.toolchain import Compiler

        compilers = [Compiler(Path("/usr/bin/g++"), "gcc", (11, 0), "g++")]
        with mock.patch("enm.doctor.detect_compilers", return_value=compilers):
            checks = _compiler_family_checks()
        gcc_check = next(c for c in checks if c.name == "gcc")
        self.assertEqual(gcc_check.status, "unsupported")
        parent = next(c for c in checks if c.name == "compiler")
        self.assertEqual(parent.status, "unsupported")


class DoctorToolchainConstraintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_manifest(self, toolchain=None):
        manifest = {
            "schema": 2,
            "name": "App",
            "version": "0.1.0",
            "target": "App",
            "eui": {"version": "v0.5.6"},
            "build_dir": "build/default",
            "toolchain": toolchain or {},
        }
        (self.tmp / "enm-project.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    def test_no_constraint_returns_empty(self):
        self._write_manifest()
        self.assertEqual(_toolchain_constraint_checks(self.tmp), [])

    def test_matching_constraint_returns_ok(self):
        from enm.toolchain import Compiler

        self._write_manifest({"compiler": "gcc", "version": ">=12"})
        current = Compiler(Path("/usr/bin/g++"), "gcc", (13, 2), "g++")
        with mock.patch("enm.toolchain.resolve_compiler", return_value=current):
            with mock.patch("enm.toolchain.detect_compilers", return_value=[current]):
                with mock.patch("os.name", "posix"):
                    checks = _toolchain_constraint_checks(self.tmp)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, "ok")

    def test_family_mismatch_returns_unsupported(self):
        from enm.toolchain import Compiler

        self._write_manifest({"compiler": "msvc", "version": ">=19.44"})
        current = Compiler(Path("/usr/bin/g++"), "gcc", (13, 2), "g++")
        with mock.patch("enm.toolchain.resolve_compiler", return_value=current):
            with mock.patch("enm.toolchain.detect_compilers", return_value=[current]):
                checks = _toolchain_constraint_checks(self.tmp)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, "unsupported")
        self.assertIn("msvc", checks[0].detail)

    def test_version_mismatch_returns_unsupported(self):
        from enm.toolchain import Compiler

        self._write_manifest({"compiler": "gcc", "version": ">=14"})
        current = Compiler(Path("/usr/bin/g++"), "gcc", (13, 2), "g++")
        with mock.patch("enm.toolchain.resolve_compiler", return_value=current):
            with mock.patch("enm.toolchain.detect_compilers", return_value=[current]):
                checks = _toolchain_constraint_checks(self.tmp)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, "unsupported")

    def test_missing_compiler_returns_missing(self):
        self._write_manifest({"compiler": "gcc", "version": ">=12"})
        with mock.patch("enm.toolchain.resolve_compiler", return_value=None):
            with mock.patch("enm.toolchain.detect_compilers", return_value=[]):
                checks = _toolchain_constraint_checks(self.tmp)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, "missing")

    def test_required_compiler_found_even_when_not_active(self):
        from enm.toolchain import Compiler

        self._write_manifest({"compiler": "clang"})
        current = Compiler(Path("C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64/cl.exe"), "msvc", (19, 44), "cl")
        clang = Compiler(Path("C:/Program Files/LLVM/bin/clang++.exe"), "clang", (19, 1), "clang++")
        with mock.patch("enm.toolchain.resolve_compiler", return_value=current):
            with mock.patch("enm.toolchain.detect_compilers", return_value=[current, clang]):
                with mock.patch("shutil.which", return_value="C:/ninja/ninja.exe"):
                    checks = _toolchain_constraint_checks(self.tmp)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, "ok")
        self.assertIn("clang", checks[0].detail)


class DoctorDependencyTests(unittest.TestCase):
    def test_ninja_required_for_non_msvc_toolchain_on_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "schema": 2,
                "name": "App",
                "version": "0.1.0",
                "target": "App",
                "eui": {"version": "v0.5.6"},
                "build_dir": "build/default",
                "toolchain": {"compiler": "clang"},
            }
            (root / "enm-project.json").write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch("os.name", "nt"):
                with mock.patch("shutil.which", return_value=None):
                    checks = _tool_versions(root)
        ninja = next(check for check in checks if check.name == "ninja")
        self.assertEqual(ninja.status, "missing")
        self.assertTrue(ninja.required)

    def test_ninja_optional_for_msvc_toolchain_on_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "schema": 2,
                "name": "App",
                "version": "0.1.0",
                "target": "App",
                "eui": {"version": "v0.5.6"},
                "build_dir": "build/default",
                "toolchain": {"compiler": "msvc"},
            }
            (root / "enm-project.json").write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch("os.name", "nt"):
                with mock.patch("shutil.which", return_value=None):
                    checks = _tool_versions(root)
        self.assertFalse(any(check.name == "ninja" for check in checks))

    def test_opengl_on_windows_is_ok(self):
        with mock.patch("os.name", "nt"):
            check = _opengl_check()
        self.assertEqual(check.status, "ok")

    def test_opengl_on_linux_uses_pkg_config(self):
        with mock.patch("os.name", "posix"):
            with mock.patch("enm.doctor._is_darwin", return_value=False):
                with mock.patch("enm.doctor._pkg_config_version", return_value="1.2.3"):
                    check = _opengl_check()
        self.assertEqual(check.status, "ok")
        self.assertIn("1.2.3", check.detail)

    def test_linux_deps_reports_missing_pkg_config(self):
        with mock.patch("enm.doctor._is_linux", return_value=True):
            with mock.patch("enm.doctor._pkg_config_version", return_value=None):
                checks = _linux_system_deps()
        x11 = next(check for check in checks if check.name == "x11")
        self.assertEqual(x11.status, "missing")
        self.assertTrue(x11.required)

    def test_vulkan_optional_when_not_required(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", return_value=None):
                check = _vulkan_check(required=False)
        self.assertEqual(check.status, "optional")
        self.assertFalse(check.required)

    def test_sdl2_required_reports_missing(self):
        with mock.patch("shutil.which", return_value=None):
            with mock.patch("enm.doctor._pkg_config_exists", return_value=False):
                check = _sdl2_check(required=True)
        self.assertEqual(check.status, "missing")
        self.assertTrue(check.required)


class DoctorSdkTests(unittest.TestCase):
    def _fake_store(self, root: Path, version: str = "v9.8.7") -> StateStore:
        store = StateStore(root)
        sdk_path = root / "sdks" / version / "windows-x64"
        sdk_path.mkdir(parents=True)
        config = sdk_path / "lib/cmake/EuiNeo/EuiNeoConfig.cmake"
        config.parent.mkdir(parents=True)
        config.write_text("function(eui_neo_configure_app target)\nendfunction()\n", encoding="utf-8")
        header = sdk_path / "include/eui_neo.h"
        header.parent.mkdir(parents=True)
        header.write_text("// header\n", encoding="utf-8")
        lib = sdk_path / "eui_neo.lib"
        lib.write_text("lib", encoding="utf-8")
        store.save(
            {
                "schema": 1,
                "active": {"windows-x64": version},
                "installed": {
                    version: {
                        "windows-x64": {
                            "path": str(sdk_path),
                            "asset": "sdk.zip",
                            "sha256": "abc",
                        }
                    }
                },
            }
        )
        return store

    def test_sdk_check_reports_completeness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._fake_store(root)
            checks = _sdk_check(store, None, deep=False)
            sdk_check = next(check for check in checks if check.name == "eui-sdk")
            self.assertEqual(sdk_check.status, "ok")
            self.assertIn("headers present", sdk_check.detail)
            self.assertIn("eui_neo_configure_app() available", sdk_check.detail)

    def test_sdk_check_reports_incompatible_msvc_abi(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._fake_store(Path(directory))
            abi = AbiResult("unsupported", "required STL symbols are unavailable")
            with mock.patch("enm.doctor.check_sdk_abi", return_value=abi):
                checks = _sdk_check(store, None, deep=False)
        check = next(check for check in checks if check.name == "msvc-sdk-abi")
        self.assertEqual(check.status, "unsupported")
        self.assertTrue(check.required)

    def test_sdk_check_reports_uninspectable_abi_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._fake_store(Path(directory))
            with mock.patch("enm.doctor.check_sdk_abi", return_value=AbiResult("unknown", "cannot inspect")):
                checks = _sdk_check(store, None, deep=False)
        check = next(check for check in checks if check.name == "msvc-sdk-abi")
        self.assertEqual(check.status, "unknown")
        self.assertFalse(check.required)

    def test_sdk_check_fails_without_entry_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._fake_store(root)
            config = next(iter((root / "sdks" / "v9.8.7" / "windows-x64").rglob("EuiNeoConfig.cmake")))
            config.write_text("# empty\n", encoding="utf-8")
            checks = _sdk_check(store, None, deep=False)
            sdk_check = next(check for check in checks if check.name == "eui-sdk")
            self.assertEqual(sdk_check.status, "error")
            self.assertIn("no supported entry point", sdk_check.detail)

    def test_run_doctor_with_missing_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root)
            with mock.patch("enm.doctor._tool_versions", return_value=[]):
                with mock.patch("enm.doctor._visual_studio_check", return_value=[]):
                    with mock.patch("enm.doctor._compiler_check", return_value=[]):
                        with mock.patch("enm.doctor._linux_system_deps", return_value=[]):
                            with mock.patch("enm.doctor._opengl_check", return_value=Check("opengl", "ok", "ok")):
                                checks = run_doctor(store)
            sdk_check = next(check for check in checks if check.name == "eui-sdk")
            self.assertEqual(sdk_check.status, "missing")
            self.assertTrue(sdk_check.required)

    def test_run_doctor_with_missing_project_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "schema": 1,
                "name": "test",
                "version": "0.1.0",
                "target": "test",
                "eui": {"version": "v99.99.99"},
                "build_dir": "build/default",
            }
            (root / "enm-project.json").write_text(json.dumps(manifest), encoding="utf-8")
            store = StateStore(root)
            with mock.patch("enm.doctor._tool_versions", return_value=[]):
                with mock.patch("enm.doctor._visual_studio_check", return_value=[]):
                    with mock.patch("enm.doctor._compiler_check", return_value=[]):
                        with mock.patch("enm.doctor._linux_system_deps", return_value=[]):
                            with mock.patch("enm.doctor._opengl_check", return_value=Check("opengl", "ok", "ok")):
                                checks = run_doctor(store, project_root=root)
            sdk_check = next(check for check in checks if check.name == "eui-sdk")
            self.assertEqual(sdk_check.status, "missing")
            self.assertIn("v99.99.99", sdk_check.detail)
            self.assertTrue(sdk_check.required)

    def test_deep_probe_uses_locked_toolchain(self):
        from unittest import mock
        from enm.toolchain import Compiler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._fake_store(root)
            manifest = {
                "schema": 2,
                "name": "test",
                "version": "0.1.0",
                "target": "test",
                "eui": {"version": "v9.8.7"},
                "build_dir": "build/default",
                "toolchain": {"compiler": "clang", "version": ""},
            }
            (root / "enm-project.json").write_text(json.dumps(manifest), encoding="utf-8")
            msvc = Compiler(Path("C:/msvc/cl.exe"), "msvc", (19, 44), "cl")
            clang = Compiler(Path("C:/llvm/clang++.exe"), "clang", (19, 1), "clang++")
            with mock.patch("enm.toolchain.resolve_compiler", return_value=msvc):
                with mock.patch("enm.toolchain.detect_compilers", return_value=[msvc, clang]):
                    with mock.patch("enm.doctor.check_sdk_abi", return_value=AbiResult("ok", "compatible")):
                        with mock.patch("shutil.which", return_value="C:/ninja/ninja.exe"):
                            with mock.patch("os.name", "nt"):
                                with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="", stderr="")) as run_mock:
                                    checks = _sdk_check(store, root, deep=True)
            probe = next(check for check in checks if check.name == "sdk-toolchain")
            self.assertEqual(probe.status, "ok")
            command = run_mock.call_args_list[0].args[0]
            self.assertIn("-DCMAKE_CXX_COMPILER=C:/llvm/clang++.exe", command)
            self.assertIn("Ninja", command)

    def test_deep_probe_reports_link_failure(self):
        sdk = InstalledSdk("v0.5.6", "windows", "x64", Path("C:/sdk"), "sdk.zip", "abc")
        compiler = Compiler(Path("C:/msvc/cl.exe"), "msvc", (19, 44), "cl")
        configured = mock.MagicMock(returncode=0, stdout="", stderr="")
        failed = mock.MagicMock(returncode=1, stdout="error LNK2019: missing symbol", stderr="")
        with mock.patch("enm.doctor.find_visual_studio", return_value=None):
            with mock.patch("enm.doctor.subprocess.run", side_effect=[configured, failed]) as run_mock:
                with mock.patch("enm.doctor.os.name", "nt"):
                    check = _deep_sdk_probe(sdk, Path("C:/sdk/lib/cmake/EuiNeo/EuiNeoConfig.cmake"), compiler)
        self.assertEqual(check.status, "unsupported")
        self.assertIn("build/link failed", check.detail)
        self.assertEqual(run_mock.call_count, 2)


class DoctorFixTests(unittest.TestCase):
    def test_detect_package_manager_finds_winget(self):
        with mock.patch("os.name", "nt"):
            with mock.patch("shutil.which", side_effect=lambda name: name == "winget"):
                self.assertEqual(_detect_package_manager(), "winget")

    def test_detect_package_manager_falls_back_on_windows(self):
        with mock.patch("os.name", "nt"):
            with mock.patch("shutil.which", return_value=None):
                self.assertIsNone(_detect_package_manager())

    def test_apt_packages_collects_missing(self):
        checks = [
            Check("x11", "missing", "", required=True),
            Check("libcurl", "missing", "", required=True),
            Check("cmake", "ok", "", required=True),
        ]
        packages = _apt_packages(checks)
        self.assertIn("libx11-dev", packages)
        self.assertIn("libcurl4-openssl-dev", packages)
        self.assertNotIn("cmake", packages)

    def test_install_command_cmake_on_windows_with_winget(self):
        check = Check("cmake", "missing", "not found", required=True)
        command, desc = _install_command(check, "winget")
        self.assertEqual(command, ["winget", "install", "Kitware.CMake"])

    def test_install_command_eui_sdk_uses_pinned_version(self):
        check = Check(
            "eui-sdk",
            "missing",
            "EUI-NEO v0.5.2 (windows-x64) is required by the project but not installed; run 'enm sdk install v0.5.2'",
            required=True,
        )
        command, desc = _install_command(check, "winget")
        self.assertEqual(command[:5], [sys.executable, "-m", "enm", "sdk", "install"])
        self.assertEqual(command[5], "v0.5.2")
        self.assertIn("v0.5.2", desc)

    def test_install_command_vulkan_on_windows_is_manual(self):
        check = Check("vulkan", "missing", "", required=True)
        command, desc = _install_command(check, "winget")
        self.assertIsNone(command)
        self.assertIn("vulkan.lunarg.com", desc)

    def test_install_command_sdl2_on_linux_apt(self):
        check = Check("sdl2", "missing", "", required=True)
        with mock.patch("os.name", "posix"):
            with mock.patch("enm.doctor._is_darwin", return_value=False):
                with mock.patch("enm.doctor._is_linux", return_value=True):
                    command, desc = _install_command(check, "apt")
        self.assertEqual(command, ["sudo", "apt", "install", "-y", "libsdl2-dev"])

    def test_run_install_command_reports_success(self):
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="done\n", stderr="")):
            success, detail = _run_install_command(["echo", "hi"])
        self.assertTrue(success)
        self.assertEqual(detail, "done")

    def test_run_install_command_reports_failure(self):
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="", stderr="not found\n")):
            success, detail = _run_install_command(["false"])
        self.assertFalse(success)
        self.assertEqual(detail, "not found")

    def test_fix_missing_dependencies_skips_already_satisfied(self):
        checks = [Check("cmake", "ok", "ok", required=True)]
        result = fix_missing_dependencies(checks, yes=True)
        self.assertEqual(result[0].status, "ok")

    def test_fix_missing_dependencies_fixes_cmake_on_windows(self):
        checks = [Check("cmake", "missing", "not found", required=True)]
        with mock.patch("os.name", "nt"):
            with mock.patch("shutil.which", return_value="winget"):
                with mock.patch("enm.doctor._run_install_command", return_value=(True, "installed")):
                    result = fix_missing_dependencies(checks, yes=True)
        self.assertEqual(result[0].status, "fixed")

    def test_fix_prompts_for_optional_dependencies_with_force(self):
        checks = [Check("ninja", "optional", "not found", required=False)]
        with mock.patch("os.name", "nt"):
            with mock.patch("shutil.which", return_value="winget"):
                with mock.patch("enm.doctor._run_install_command", return_value=(True, "installed")):
                    with mock.patch("builtins.input", return_value="y"):
                        result = fix_missing_dependencies(checks, force=True)
        self.assertEqual(result[0].status, "fixed")

    def test_fix_does_not_offer_optional_without_force(self):
        checks = [Check("ninja", "optional", "not found", required=False)]
        with mock.patch("os.name", "nt"):
            with mock.patch("shutil.which", return_value="winget"):
                with mock.patch("enm.doctor._run_install_command") as run_mock:
                    with mock.patch("builtins.input") as input_mock:
                        result = fix_missing_dependencies(checks)
        run_mock.assert_not_called()
        input_mock.assert_not_called()
        self.assertEqual(result[0].status, "optional")

    def test_fix_prompts_required_items_one_by_one(self):
        checks = [
            Check("cmake", "missing", "not found", required=True),
            Check("ninja", "missing", "not found", required=True),
        ]
        with mock.patch("os.name", "nt"):
            with mock.patch("shutil.which", return_value="winget"):
                with mock.patch("enm.doctor._run_install_command", return_value=(True, "installed")) as run_mock:
                    with mock.patch("builtins.input", return_value="y"):
                        result = fix_missing_dependencies(checks)
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(result[0].status, "fixed")
        self.assertEqual(result[1].status, "fixed")

    def test_fix_suggests_switching_for_family_mismatch(self):
        from io import StringIO

        checks = [Check("toolchain", "unsupported", "project requires msvc >=19.44,<20 but current compiler is gcc", required=True)]
        with mock.patch("sys.stdout", new=StringIO()) as output:
            fix_missing_dependencies(checks, yes=False, force=False)
        text = output.getvalue()
        self.assertIn("Project requires MSVC but the active compiler is GCC", text)
        self.assertIn("enm lock-compiler", text)
        self.assertIn("enm configure --force", text)

    def test_fix_suggests_installing_required_compiler_when_missing(self):
        from io import StringIO

        checks = [Check("toolchain", "missing", "project requires clang but no compiler was found", required=True)]
        with mock.patch("sys.stdout", new=StringIO()) as output:
            fix_missing_dependencies(checks, yes=False, force=False)
        text = output.getvalue()
        self.assertIn("Toolchain constraint requires CLANG but no matching compiler was found", text)
        self.assertIn("winget install LLVM.LLVM", text)
        self.assertIn("enm lock-compiler", text)
        self.assertIn("enm configure --force", text)

    def test_fix_does_not_misidentify_current_compiler_as_required(self):
        from io import StringIO

        checks = [Check("toolchain", "unsupported", "project requires clang but current compiler is msvc", required=True)]
        with mock.patch("sys.stdout", new=StringIO()) as output:
            fix_missing_dependencies(checks, yes=False, force=False)
        text = output.getvalue()
        self.assertIn("Project requires CLANG but the active compiler is MSVC", text)
        self.assertNotIn("MSVC must be installed manually", text)
        self.assertNotIn("Toolchain constraint requires MSVC", text)


class DoctorCliTests(unittest.TestCase):
    def test_doctor_parser_accepts_project_and_deep(self):
        from enm.cli import parser

        args = parser().parse_args(["doctor", "--project", ".", "--deep"])
        self.assertEqual(args.project, ".")
        self.assertTrue(args.deep)

    def test_doctor_fix_parser_accepts_yes(self):
        from enm.cli import parser

        args = parser().parse_args(["doctor", "fix", "--yes"])
        self.assertEqual(args.doctor_command, "fix")
        self.assertTrue(args.yes)

    def test_doctor_fix_parser_accepts_project_and_deep(self):
        from enm.cli import parser

        args = parser().parse_args(["doctor", "fix", "--project", ".", "--deep"])
        self.assertEqual(args.project, ".")
        self.assertTrue(args.deep)

    def test_doctor_fix_parser_accepts_json_after_subcommand(self):
        from enm.cli import parser

        args = parser().parse_args(["doctor", "fix", "--json"])
        self.assertTrue(args.json)

    def test_doctor_fix_rejects_yes_and_force_together(self):
        from enm.cli import cmd_doctor_fix

        args = mock.MagicMock()
        args.yes = True
        args.force = True
        args.project = None
        args.deep = False
        args.home = None
        args.json = False
        self.assertEqual(cmd_doctor_fix(args), 2)


class BackendDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_manifest(self, build_dir="build/default"):
        manifest = {
            "schema": 1,
            "name": "test",
            "version": "0.1.0",
            "target": "test",
            "eui": {"version": "v0.2.0"},
            "build_dir": build_dir,
        }
        (self.tmp / "enm-project.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_defaults_without_project(self):
        window, render, fetch = _detect_backends(None)
        self.assertEqual(window, "glfw")
        self.assertEqual(render, "opengl")
        self.assertFalse(fetch)

    def test_defaults_with_project(self):
        self._write_manifest()
        window, render, fetch = _detect_backends(self.tmp)
        self.assertEqual(window, "glfw")
        self.assertEqual(render, "opengl")
        self.assertFalse(fetch)

    def test_detects_from_cmake_lists(self):
        self._write_manifest()
        (self.tmp / "CMakeLists.txt").write_text(
            'set(EUI_WINDOW_BACKEND "SDL2")\nset(EUI_RENDER_BACKEND "Vulkan")\nset(EUI_DEPS_MODE "fetch")\n',
            encoding="utf-8",
        )
        window, render, fetch = _detect_backends(self.tmp)
        self.assertEqual(window, "sdl2")
        self.assertEqual(render, "vulkan")
        self.assertTrue(fetch)

    def test_cmake_cache_overrides_lists(self):
        self._write_manifest()
        (self.tmp / "CMakeLists.txt").write_text(
            'set(EUI_WINDOW_BACKEND "SDL2")\nset(EUI_RENDER_BACKEND "Vulkan")\n',
            encoding="utf-8",
        )
        build_dir = self.tmp / "build/default"
        build_dir.mkdir(parents=True)
        (build_dir / "CMakeCache.txt").write_text(
            "EUI_WINDOW_BACKEND:STRING=glfw\nEUI_RENDER_BACKEND:STRING=opengl\n",
            encoding="utf-8",
        )
        window, render, fetch = _detect_backends(self.tmp)
        self.assertEqual(window, "glfw")
        self.assertEqual(render, "opengl")
        self.assertFalse(fetch)

    def test_legacy_build_dir_fallback(self):
        self._write_manifest("build/sdl2-vulkan")
        window, render, fetch = _detect_backends(self.tmp)
        self.assertEqual(window, "sdl2")
        self.assertEqual(render, "vulkan")
        self.assertFalse(fetch)


if __name__ == "__main__":
    unittest.main()
