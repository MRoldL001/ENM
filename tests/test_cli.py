from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from enm.cli import parser
from enm.github import normalize_arch, normalize_platform
from enm.state import StateStore


class CliTests(unittest.TestCase):
    def test_doctor_required_missing_check_returns_failure(self):
        from enm.cli import _print_checks
        from enm.doctor import Check

        with contextlib.redirect_stdout(io.StringIO()):
            code = _print_checks([Check("cmake", "missing", "not found", required=True)])
        self.assertEqual(code, 1)

    def test_sdk_list_accepts_specific_version(self):
        args = parser().parse_args(["sdk", "list", "v0.5.6"])
        self.assertEqual(args.version, "v0.5.6")

    def test_sdk_commands_do_not_expose_host_overrides(self):
        for command in (["sdk", "list"], ["sdk", "install"], ["sdk", "path"]):
            with self.assertRaises(SystemExit):
                parser().parse_args(command + ["--platform", "linux"])

    def test_lock_compiler_parser_registered(self):
        args = parser().parse_args(["lock-compiler"])
        self.assertEqual(args.func.__name__, "cmd_lock_compiler")

    def test_lock_compiler_writes_toolchain_to_manifest(self):
        from unittest import mock
        from enm.project import create_project
        from enm.toolchain import Compiler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            create_project(root, "App", "v0.5.6")
            gcc = Compiler(Path("/usr/bin/g++"), "gcc", (13, 2), "g++")
            args = parser().parse_args(["lock-compiler", "--project", str(root)])
            with mock.patch("enm.cli.detect_compilers", return_value=[gcc]):
                self.assertEqual(args.func(args), 0)
            manifest = json.loads((root / "enm-project.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["toolchain"]["compiler"], "gcc")
        self.assertEqual(manifest["toolchain"]["version"], "=13.2")

    def test_lock_compiler_filters_out_below_minimum_versions(self):
        from unittest import mock
        from enm.project import create_project
        from enm.toolchain import Compiler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            create_project(root, "App", "v0.5.6")
            gcc = Compiler(Path("C:/mingw64/bin/g++.EXE"), "gcc", (8, 1), "g++")
            clang = Compiler(Path("C:/Program Files/LLVM/bin/clang++.exe"), "clang", (19, 1), "clang++")
            args = parser().parse_args(["lock-compiler", "--project", str(root)])
            with mock.patch("enm.cli.detect_compilers", return_value=[gcc, clang]):
                with mock.patch("builtins.input", return_value="1"):
                    self.assertEqual(args.func(args), 0)
            manifest = json.loads((root / "enm-project.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["toolchain"]["compiler"], "clang")
        self.assertEqual(manifest["toolchain"]["version"], "=19.1")

    def test_lock_compiler_rejects_all_when_none_meet_minimum(self):
        from unittest import mock
        from enm.project import create_project
        from enm.toolchain import Compiler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            create_project(root, "App", "v0.5.6")
            gcc = Compiler(Path("C:/mingw64/bin/g++.EXE"), "gcc", (8, 1), "g++")
            args = parser().parse_args(["lock-compiler", "--project", str(root)])
            with mock.patch("enm.cli.detect_compilers", return_value=[gcc]):
                self.assertEqual(args.func(args), 2)

    def test_lock_compiler_handles_single_part_version(self):
        from unittest import mock
        from enm.project import create_project
        from enm.toolchain import Compiler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            create_project(root, "App", "v0.5.6")
            gcc = Compiler(Path("/usr/bin/g++"), "gcc", (13,), "g++")
            args = parser().parse_args(["lock-compiler", "--project", str(root)])
            with mock.patch("enm.cli.detect_compilers", return_value=[gcc]):
                self.assertEqual(args.func(args), 0)
            manifest = json.loads((root / "enm-project.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["toolchain"]["compiler"], "gcc")
        self.assertEqual(manifest["toolchain"]["version"], "=13")

    def test_doctor_fix_preserves_fixed_status_after_rerun(self):
        from unittest import mock
        from enm.doctor import Check

        missing = Check("cmake", "missing", "not found", required=True)
        fixed = Check("cmake", "fixed", "installed: install CMake", required=True)
        ok = Check("cmake", "ok", "cmake version 3.29.1", required=True)
        args = parser().parse_args(["doctor", "fix", "--yes"])
        with mock.patch("enm.cli.run_doctor", side_effect=[[missing], [ok]]):
            with mock.patch("enm.cli.fix_missing_dependencies", return_value=[fixed]):
                with mock.patch("enm.cli._print_checks", return_value=1) as print_mock:
                    self.assertEqual(args.func(args), 1)
        printed_checks = print_mock.call_args[0][0]
        cmake = next(check for check in printed_checks if check.name == "cmake")
        self.assertEqual(cmake.status, "fixed")

    def test_doctor_fix_json_requires_noninteractive_confirmation(self):
        args = parser().parse_args(["doctor", "fix", "--json"])
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(args.func(args), 2)
        self.assertIn("requires --yes", error.getvalue())

    def test_doctor_fix_json_keeps_stdout_machine_readable(self):
        from unittest import mock
        from enm.doctor import Check

        missing = Check("cmake", "missing", "not found", required=True)
        args = parser().parse_args(["doctor", "fix", "--json", "--yes"])
        output = io.StringIO()
        error = io.StringIO()
        with mock.patch("enm.cli.run_doctor", return_value=[missing]):
            with mock.patch("enm.cli.fix_missing_dependencies", side_effect=lambda checks, **kwargs: (print("installing"), checks)[1]):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    self.assertEqual(args.func(args), 1)
        self.assertEqual(json.loads(output.getvalue())[0]["name"], "cmake")
        self.assertIn("installing", error.getvalue())

    def test_sdk_installed_lists_local_state_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = f"{normalize_platform()}-{normalize_arch()}"
            store = StateStore(root)
            store.save(
                {
                    "schema": 1,
                    "active": {key: "v1.2.3"},
                    "installed": {
                        "v1.2.3": {
                            key: {"path": str(root / "sdk"), "asset": "sdk.zip", "sha256": "abc"}
                        }
                    },
                }
            )
            args = parser().parse_args(["--home", str(root), "sdk", "installed"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            self.assertIn("v1.2.3", output.getvalue())
            self.assertIn(str(root / "sdk"), output.getvalue())


if __name__ == "__main__":
    unittest.main()
