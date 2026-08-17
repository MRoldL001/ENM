from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from enm.toolchain import Compiler, VersionConstraint, detect_compilers, resolve_compiler


class VersionConstraintTests(unittest.TestCase):
    def test_single_minimum(self):
        c = VersionConstraint.parse(">=19.44")
        self.assertTrue(c.match((19, 44)))
        self.assertTrue(c.match((19, 45)))
        self.assertFalse(c.match((19, 43)))

    def test_range(self):
        c = VersionConstraint.parse(">=19.44,<20")
        self.assertTrue(c.match((19, 44)))
        self.assertTrue(c.match((19, 50)))
        self.assertFalse(c.match((20, 0)))
        self.assertFalse(c.match((19, 43)))

    def test_exact(self):
        c = VersionConstraint.parse("=14.0")
        self.assertTrue(c.match((14, 0)))
        self.assertFalse(c.match((14, 1)))

    def test_greater_than(self):
        c = VersionConstraint.parse(">19.44")
        self.assertFalse(c.match((19, 44)))
        self.assertTrue(c.match((19, 45)))

    def test_less_than_or_equal(self):
        c = VersionConstraint.parse("<=19.44")
        self.assertTrue(c.match((19, 44)))
        self.assertTrue(c.match((19, 43)))
        self.assertFalse(c.match((19, 45)))

    def test_str(self):
        self.assertEqual(str(VersionConstraint.parse(">=19.44,<20")), ">=19.44,<20")


class CompilerDetectionTests(unittest.TestCase):
    def test_detect_compilers_returns_priority_order_on_windows(self):
        vs = mock.MagicMock()
        vs.path = Path("/vs")
        vs.toolset_version = "14.44"
        with mock.patch("enm.toolchain.find_visual_studio", return_value=vs):
            with mock.patch("os.name", "nt"):
                with mock.patch(
                    "shutil.which",
                    side_effect=lambda name: f"/usr/bin/{name}" if name in ("g++", "clang++") else None,
                ):
                    with mock.patch("pathlib.Path.is_file", return_value=True):
                        with mock.patch("enm.toolchain._compiler_version", return_value=((19, 44), "cl")):
                            compilers = detect_compilers()
        self.assertEqual([c.family for c in compilers], ["msvc", "gcc", "clang"])

    def test_detect_compilers_skips_cl_when_not_present(self):
        with mock.patch("enm.toolchain.find_visual_studio", return_value=None):
            with mock.patch(
                "shutil.which",
                side_effect=lambda name: f"/usr/bin/{name}" if name in ("g++", "clang++") else None,
            ):
                compilers = detect_compilers()
        self.assertEqual([c.family for c in compilers], ["gcc", "clang"])

    def test_resolve_compiler_prefers_cxx_env(self):
        with mock.patch.dict(os.environ, {"CXX": "/usr/bin/clang++"}, clear=True):
            with mock.patch("shutil.which", return_value="/usr/bin/clang++"):
                with mock.patch("enm.toolchain.find_visual_studio", return_value=None):
                    compiler = resolve_compiler()
        self.assertIsNotNone(compiler)
        self.assertEqual(compiler.family, "clang")

    def test_resolve_compiler_returns_none_when_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", return_value=None):
                with mock.patch("enm.toolchain.find_visual_studio", return_value=None):
                    compiler = resolve_compiler()
        self.assertIsNone(compiler)

    def test_compiler_version_falls_back_to_toolset_path_for_msvc(self):
        from enm.toolchain import _compiler_version

        path = Path("C:/VS/VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64/cl.exe")
        version, detail = _compiler_version(path, "msvc")
        self.assertEqual(version, (19, 44))
        self.assertIn("14.44", detail)
