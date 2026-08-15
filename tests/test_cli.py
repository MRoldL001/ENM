from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from enm.cli import parser
from enm.github import normalize_arch, normalize_platform
from enm.state import StateStore


class CliTests(unittest.TestCase):
    def test_sdk_list_accepts_specific_version(self):
        args = parser().parse_args(["sdk", "list", "v0.5.6"])
        self.assertEqual(args.version, "v0.5.6")

    def test_sdk_commands_do_not_expose_host_overrides(self):
        for command in (["sdk", "list"], ["sdk", "install"], ["sdk", "path"]):
            with self.assertRaises(SystemExit):
                parser().parse_args(command + ["--platform", "linux"])

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
