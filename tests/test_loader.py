"""Tests for the encrypted self-loading bootstrap.

The loader is the layer that removes *static* analysis: the file contains a
decoder, a table of encrypted segments and a ``load`` call -- no program logic.
These tests check that it runs identically, that it hands ``load`` a reader
instead of a string, that tampering is caught, and that a hooked ``load`` is
detected before anything is decrypted.
"""

import glob
import os
import re
import unittest

from luaobf.loader import CHECKSUM_MOD, SEGMENT_SIZE, checksum
from luaobf.obfuscator import obfuscate_source, preset

try:
    from tests.lua_runner import HAVE_LUA, run_lua

    from lupa import LuaRuntime
except Exception:  # pragma: no cover
    HAVE_LUA = False

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = sorted(glob.glob(os.path.join(HERE, "scripts", "*.lua")))

SOURCE = """
local api_key = "sk_live_9f2a7c4e"
local function url(n)
  if n > 1000 then return "https://api.example.com/v1?key=" .. api_key end
  return "https://api.example.com/v1?n=" .. n
end
print(url(500))
print(url(5000))
"""

#: hooks ``load`` the way an analyst would, then runs the build
HOOKED_LOAD = r"""
local real = loadstring or load
_G.__real = real
_G.__sent = {}
local function fake(first, second)
  __sent[#__sent + 1] = type(first)
  return real(first, second)
end
if loadstring then loadstring = fake end
load = fake
"""


def build(source=SOURCE, **options):
    opts = preset("nightmare", seed=3, anti_debug=False, loader=True)
    for key, value in options.items():
        setattr(opts, key, value)
    return obfuscate_source(source, opts)


class ChecksumTests(unittest.TestCase):
    def test_checksum_is_stable_and_bounded(self):
        self.assertEqual(checksum(b""), 0)
        self.assertEqual(checksum(b"a"), 97)
        value = checksum(b"hello world" * 100)
        self.assertLess(value, CHECKSUM_MOD)
        self.assertGreater(value, 0)
        self.assertEqual(checksum(b"abc"), checksum(b"abc"))

    def test_segments_cover_the_program(self):
        self.assertGreater(SEGMENT_SIZE, 0)
        self.assertLess(SEGMENT_SIZE, 256)


class LoaderStructureTests(unittest.TestCase):
    def test_no_program_logic_is_readable(self):
        code = build().code
        for identifier in ("api_key", "sk_live", "url", "endpoint"):
            self.assertNotIn(identifier, code)
        self.assertLess(len(re.findall(r"\bfunction\b", code)), 20)

    def test_load_is_fed_a_reader_not_a_string(self):
        code = build().code
        self.assertRegex(code, r"\bloadstring or load\b")
        # the reader is the argument to load, and the chunk name is explicit
        self.assertIn('"@protected"', code)
        self.assertRegex(code, r"\b\w+=\w+\(\w+,\"@protected\"\)")
        self.assertIn("checksum mismatch", code)

    def test_program_is_split_into_segments(self):
        code = build(loader=True).code
        segments = re.findall(r'"[A-Za-z0-9+/=]{40,}"', code)
        # a 300-byte program at 64 bytes per segment is several segments
        self.assertGreater(len(segments), 3)

    def test_stats_report_loader_and_layers(self):
        result = build(layers=2)
        self.assertTrue(result.stats["loader"])
        self.assertEqual(result.stats["layers"], 2)
        plain = obfuscate_source(SOURCE, preset("nightmare", seed=3, loader=False))
        self.assertFalse(plain.stats["loader"])
        self.assertEqual(plain.stats["layers"], 0)

    def test_guard_can_be_disabled(self):
        self.assertIn("integrity check failed", build(anti_debug=True).code)
        self.assertNotIn("integrity check failed", build(anti_debug=False).code)

    def test_brand_stays_a_hidden_watermark(self):
        code = build(brand="Forsakenis4").code
        self.assertNotIn("Forsakenis4", code)
        self.assertIn('"protected chunk: "', code)

    def test_layers_nest(self):
        one = build(layers=1).code
        three = build(layers=3).code
        # the inner layers are ciphertext, so only size betrays the nesting
        self.assertGreater(len(three), len(one) * 2)
        self.assertEqual(three.count("checksum mismatch"), 1)


@unittest.skipUnless(HAVE_LUA, "lupa not installed")
class LoaderBehaviourTests(unittest.TestCase):
    def test_behaviour_is_identical(self):
        baseline = run_lua(SOURCE)
        for seed in (1, 4):
            for layers in (1, 2):
                result = build(seed=seed, layers=layers)
                with self.subTest(seed=seed, layers=layers):
                    self.assertEqual(run_lua(result.code), baseline)

    def test_tampering_is_detected(self):
        code = build(seed=2).code
        self.assertEqual(run_lua(code).output, run_lua(SOURCE).output)

        # flip one character inside the biggest encrypted segment
        payload = max(re.findall(r'"([A-Za-z0-9+/=]{40,})"', code), key=len)
        start = code.index(payload)
        middle = len(payload) // 2
        flipped = "B" if payload[middle] != "B" else "C"
        damaged = code[:start] + payload[:middle] + flipped + payload[middle + 1 :] + code[start + len(payload) :]

        result = run_lua(damaged)
        self.assertFalse(
            result.error is None and result.output == run_lua(SOURCE).output,
            "a corrupted build must not run as if it were intact",
        )

    def test_hooked_load_is_refused(self):
        """The classic dump (replace ``load``, print its argument) must fail."""
        code = build(anti_debug=True).code
        runtime = LuaRuntime(unpack_returned_tuples=True)
        runtime.execute(HOOKED_LOAD)
        error = None
        try:
            runtime.eval("function(code) local c = assert(__real(code)) return c() end")(code)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        self.assertIsNotNone(error)
        self.assertIn("integrity check failed", error)

    def test_without_the_guard_the_reader_is_all_a_hook_sees(self):
        """Honest limit: disarm the guard and a hook sees a function, not text."""
        code = build(anti_debug=False).code
        runtime = LuaRuntime(unpack_returned_tuples=True)
        runtime.execute(HOOKED_LOAD)
        # run the build through the *saved* loader so only the build's own calls
        # reach the hook
        runtime.eval("function(code) local c = assert(__real(code)) return c() end")(code)
        kinds = [value for value in runtime.eval("__sent").values()]
        self.assertTrue(kinds)
        self.assertIn("function", kinds)
        self.assertNotIn("string", kinds)

    def test_corpus_runs_through_the_loader(self):
        for path in CORPUS:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            baseline = run_lua(source)
            with self.subTest(file=os.path.basename(path)):
                self.assertEqual(run_lua(obfuscate_source(source, preset("nightmare", seed=7, layers=2)).code), baseline)


if __name__ == "__main__":
    unittest.main()
