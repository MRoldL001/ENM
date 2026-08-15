from __future__ import annotations

import unittest

from enm.msvc import _provided_std_symbols, _undefined_std_symbols


class MsvcTests(unittest.TestCase):
    def test_extracts_only_undefined_std_symbols(self):
        output = """
43D 00000000 UNDEF notype () External | __std_search_1
111 00000000 SECT1 notype () External | __std_defined_here
222 00000000 UNDEF notype () External | ordinary_symbol
"""
        self.assertEqual(_undefined_std_symbols(output), {"__std_search_1"})

    def test_extracts_runtime_members(self):
        output = "001 __std_search_1\n002 __std_find_end_1\n"
        self.assertEqual(
            _provided_std_symbols(output), {"__std_search_1", "__std_find_end_1"}
        )


if __name__ == "__main__":
    unittest.main()
