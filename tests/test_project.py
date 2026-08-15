from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from enm.package import package_stage
from enm.project import create_project, generate_ci, load_manifest


class ProjectTests(unittest.TestCase):
    def test_project_is_pinned_to_release_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample"
            create_project(root, "Sample App", "v9.8.7")
            manifest = load_manifest(root)
            self.assertEqual(manifest["eui"]["version"], "v9.8.7")
            cmake = (root / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn("find_package(EuiNeo 9.8.7 EXACT CONFIG REQUIRED)", cmake)
            self.assertFalse((root / "CMakePresets.json").exists())
            self.assertFalse((root / ".github/workflows/build.yml").exists())

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
