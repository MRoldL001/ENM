from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from enm.github import ReleaseError
from enm.state import StateStore


class StateTests(unittest.TestCase):
    def _store_with_sdk(self, root: Path) -> StateStore:
        store = StateStore(root)
        path = root / "sdks/v1.2.3/windows-x64"
        path.mkdir(parents=True)
        store.save(
            {
                "schema": 1,
                "active": {"windows-x64": "v1.2.3"},
                "installed": {
                    "v1.2.3": {
                        "windows-x64": {
                            "path": str(path),
                            "asset": "sdk.zip",
                            "sha256": "abc",
                        }
                    }
                },
            }
        )
        return store

    def test_refuses_to_remove_active_sdk_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_sdk(Path(directory))
            with self.assertRaises(ReleaseError):
                store.uninstall("v1.2.3", "windows", "x64")

    def test_force_removes_sdk_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_sdk(Path(directory))
            removed = store.uninstall("v1.2.3", "windows", "x64", force=True)
            self.assertFalse(removed.exists())
            state = store.load()
            self.assertNotIn("v1.2.3", state["installed"])
            self.assertNotIn("windows-x64", state["active"])


if __name__ == "__main__":
    unittest.main()
