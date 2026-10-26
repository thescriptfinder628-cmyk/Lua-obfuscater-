"""Tests for the encrypted self-loading bootstrap.

The loader is the layer that removes *static* analysis: the file contains a
decoder, a blob and a ``load`` call -- no program logic at all.  These tests
check that it runs identically, that tampering is detected, and that the build
really has no readable program in it.
"""

import glob
import os
import re
import unittest

from luaobf.loader import CHECKSUM_MOD, build_loader, checksum
from luaobf.obfuscator import obfuscate_source, preset
from luaobf.renamer import NameGenerator

try:
    from tests.lua_runner import HAVE_LUA, run_lua
except Exception:  # pragma: no cover
    HAVE_LUA = False

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = sorted(glob.glob(os.path.join(HERE, "scripts", "*.lua")))

SOURCE = '''
local api_key = "sk_live_9f2a7c4e"
local function url(n)
  if n > 1000 then return "https://api.example.com/v1?key=" .. api_key end
  return "https://api.example.com/v1?n=" .. n
end
print(url(500))
print(url(5000))
'''


class ChecksumTests(unittest.TestCase):
    def test_checksum_is_stable_and_bounded(self):
        self.assertEqual(checksum(b""), 0)
        self.assertEqual(checksum(b"a"), 97)
        value = checksum(b"hello world" * 100)
        self.assertLess(value, CHECKSUM_MOD)
        self.assertGreater(value, 0)
        self.assertEqual(checksum(b"abc"), checksum(b"abc"))

    @unittest.skipUnless(HAVE_LUA, "lupa not installed")
    def test_checksum_matches_lua(self):
        data = bytes(range(256)) * 3
        ngen = NameGenerator("hash", set(), 0)
        code = build_loader("print('x')", ngen, __import__("random").Random(1), anti_debug=False)
        # extract the payload and check Python's value against Lua's loop
        payload = re.search(r'=(\w+)"([A-Za-z0-9+/=]+)"', code).group(2)
        from lupa import LuaRuntime

        runtime = LuaRuntime(unpack_returned_tuples=True)
        runtime.globals().P = payload
        lua_value = runtime.eval(
            "(function() local s=0 for i=1,#P do s=(s*31+(P):byte(i))%2147483647 end return s end)()"
        )
        self.assertEqual(lua_value, checksum(payload.encode("ascii")))
        self.assertGreater(len(data), 0)


class LoaderStructureTests(unittest.TestCase):
    def build(self, source=SOURCE, **options):
        opts = preset("nightmare", seed=3, anti_debug=False, loader=True)
        for key, value in options.items():
            setattr(opts, key, value)
        return obfuscate_source(source, opts)

    def test_no_program_logic_is_readable(self):
        code = self.build().code
        for identifier in ("api_key", "sk_live", "charge", "url", "endpoint"):
            self.assertNotIn(identifier, code)
        # the only functions are the decoder and its helpers
        self.assertLess(len(re.findall(r"\bfunction\b", code)), 12)

    def test_bootstrap_uses_load_not_a_visible_chunk(self):
        code = self.build(anti_debug=True).code
        self.assertRegex(code, r"\bloadstring or load\b")
        self.assertIn("checksum mismatch", code)
        self.assertIn("integrity check failed", code)

    def test_guard_can_be_disabled(self):
        code = self.build(anti_debug=False).code
        self.assertNotIn("integrity check failed", code)

    def test_stats_report_loader(self):
        self.assertTrue(self.build().stats["loader"])
        opts = preset("nightmare", seed=3, loader=False)
        self.assertFalse(obfuscate_source(SOURCE, opts).stats["loader"])

    def test_brand_stays_a_hidden_watermark(self):
        # the brand must not be greppable: errors use a generic prefix instead
        code = self.build(brand="Forsakenis4").code
        self.assertNotIn("Forsakenis4", code)
        self.assertIn('"protected chunk: "', code)


@unittest.skipUnless(HAVE_LUA, "lupa not installed")
class LoaderBehaviourTests(unittest.TestCase):
    def test_behaviour_is_identical(self):
        baseline = run_lua(SOURCE)
        for seed in (1, 4, 11):
            opts = preset("nightmare", seed=seed, anti_debug=False, loader=True)
            result = obfuscate_source(SOURCE, opts)
            actual = run_lua(result.code)
            with self.subTest(seed=seed):
                self.assertEqual(actual.output, baseline.output)
                self.assertIsNone(actual.error)

    def test_tampering_is_detected(self):
        opts = preset("nightmare", seed=2, anti_debug=False, loader=True)
        code = obfuscate_source(SOURCE, opts).code
        self.assertEqual(run_lua(code).output, run_lua(SOURCE).output)

        # flip one character inside the encrypted blob (the longest base64 run:
        # the short ones are the decoder's alphabet table)
        candidates = re.findall(r'"([A-Za-z0-9+/=]{100,})"', code)
        self.assertTrue(candidates)
        payload = max(candidates, key=len)
        match = re.search(re.escape(payload), code)
        middle = len(payload) // 2
        flipped = ("B" if payload[middle] != "B" else "C")
        damaged = (
            code[: match.start()]
            + payload[:middle]
            + flipped
            + payload[middle + 1 :]
            + code[match.start() + len(payload) :]
        )
        result = run_lua(damaged)
        self.assertIsNotNone(result.error)
        self.assertIn("protected chunk", result.error)
        self.assertEqual(result.output, [])

    def test_corpus_runs_through_the_loader(self):
        for path in CORPUS:
            source = open(path).read()
            baseline = run_lua(source)
            with self.subTest(file=os.path.basename(path)):
                opts = preset("nightmare", seed=7, anti_debug=False, loader=True)
                actual = run_lua(obfuscate_source(source, opts).code)
                self.assertEqual(actual.output, baseline.output)


if __name__ == "__main__":
    unittest.main()
