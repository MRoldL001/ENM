from __future__ import annotations

import io
import unittest

from enm.ui import LEGACY_CLOVER_FRAMES, UTF8_CLOVER_FRAMES, Spinner, clover_frames


class UiTests(unittest.TestCase):
    def test_uses_full_clover_when_encoding_supports_it(self):
        self.assertEqual(clover_frames("utf-8"), UTF8_CLOVER_FRAMES)
        self.assertEqual(UTF8_CLOVER_FRAMES, (".", "·", "+", "✣", "✤", "✣", "+", "·"))

    def test_uses_legacy_clover_for_gbk_terminal(self):
        self.assertEqual(clover_frames("gbk"), LEGACY_CLOVER_FRAMES)

    def test_spinner_is_quiet_when_output_is_redirected(self):
        output = io.StringIO()
        with Spinner("waiting", stream=output, interval=0.001):
            pass
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
