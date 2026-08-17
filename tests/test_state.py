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

    def test_install_does_not_change_existing_active_sdk(self):
        from unittest import mock
        from enm.github import Asset, Release

        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_sdk(Path(directory))
            release = Release(
                tag="v9.9.9",
                name="v9.9.9",
                published_at="2026-01-01T00:00:00Z",
                prerelease=False,
                assets=(Asset("eui-neo-windows-x64-sdk.zip", "", 0, None),),
                page_url="",
            )

            def _fake_extract(archive: Path, destination: Path) -> None:
                config = destination / "EuiNeoConfig.cmake"
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text("", encoding="utf-8")

            with mock.patch("enm.state.download_asset"):
                with mock.patch("enm.state.verify_digest", return_value="sha256"):
                    with mock.patch("enm.state.extract_archive", side_effect=_fake_extract):
                        installed = store.install(release, "windows", "x64")
            self.assertEqual(installed.version, "v9.9.9")
            state = store.load()
            self.assertEqual(state["active"]["windows-x64"], "v1.2.3")

    def test_install_activates_when_no_active_sdk(self):
        from unittest import mock
        from enm.github import Asset, Release

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            release = Release(
                tag="v9.9.9",
                name="v9.9.9",
                published_at="2026-01-01T00:00:00Z",
                prerelease=False,
                assets=(Asset("eui-neo-windows-x64-sdk.zip", "", 0, None),),
                page_url="",
            )

            def _fake_extract(archive: Path, destination: Path) -> None:
                config = destination / "EuiNeoConfig.cmake"
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text("", encoding="utf-8")

            with mock.patch("enm.state.download_asset"):
                with mock.patch("enm.state.verify_digest", return_value="sha256"):
                    with mock.patch("enm.state.extract_archive", side_effect=_fake_extract):
                        installed = store.install(release, "windows", "x64")
            self.assertEqual(installed.version, "v9.9.9")
            state = store.load()
            self.assertEqual(state["active"]["windows-x64"], "v9.9.9")


if __name__ == "__main__":
    unittest.main()
