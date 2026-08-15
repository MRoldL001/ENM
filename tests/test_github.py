from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from enm.github import (
    Asset,
    Release,
    ReleaseClient,
    ReleaseError,
    extract_archive,
    normalize_arch,
    normalize_platform,
    select_asset,
    verify_digest,
)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ReleaseTests(unittest.TestCase):
    def test_release_list_comes_from_api(self):
        payload = [
            {
                "tag_name": "v9.8.7",
                "name": "release",
                "published_at": "2026-01-01T00:00:00Z",
                "prerelease": False,
                "draft": False,
                "html_url": "https://github.com/example/release",
                "assets": [
                    {
                        "name": "EUI-NEO-v9.8.7-windows-x64-sdk.zip",
                        "browser_download_url": "https://github.com/example/sdk.zip",
                        "size": 12,
                        "digest": "sha256:" + "0" * 64,
                    }
                ],
            }
        ]

        def opener(request, timeout=0):
            return Response(json.dumps(payload).encode())

        releases = ReleaseClient(opener=opener).list_releases()
        self.assertEqual(releases[0].tag, "v9.8.7")
        self.assertEqual(releases[0].assets[0].digest, "sha256:" + "0" * 64)

    def test_asset_selection(self):
        wanted = Asset("EUI-NEO-v1.0.0-linux-x64-sdk.tar.gz", "https://github.com/x", 1, None)
        release = Release("v1.0.0", "", "", False, (wanted,), "")
        self.assertEqual(select_asset(release, "sdk", "linux", "x64"), wanted)
        with self.assertRaises(ReleaseError):
            select_asset(release, "sdk", "windows", "x64")

    def test_digest_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asset"
            path.write_bytes(b"verified")
            digest = hashlib.sha256(b"verified").hexdigest()
            self.assertEqual(verify_digest(path, f"sha256:{digest}"), digest)
            with self.assertRaises(ReleaseError):
                verify_digest(path, "sha256:" + "0" * 64)

    def test_zip_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr("../outside", "bad")
            with self.assertRaises(ReleaseError):
                extract_archive(archive, Path(directory) / "out")

    def test_platform_aliases(self):
        self.assertEqual(normalize_platform("Darwin"), "macos")
        self.assertEqual(normalize_arch("AMD64"), "x64")


if __name__ == "__main__":
    unittest.main()
