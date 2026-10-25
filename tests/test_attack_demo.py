"""The attack tool is part of the test suite: protection claims must be measured.

These tests assert the *security* properties we actually rely on:

* cheap dumpers (``string.char`` / ``table.concat`` hooks) recover literals from
  a cipher build -- so a cipher build is not protection on its own;
* the same dumpers recover nothing from a hardened VM build;
* observing program output still leaks everything -- so we never pretend the
  tool makes reverse-engineering impossible.
"""

import importlib.util
import os
import unittest

from luaobf.obfuscator import obfuscate_source, preset

HERE = os.path.dirname(os.path.abspath(__file__))
ATTACK = os.path.join(os.path.dirname(HERE), "tools", "attack_demo.py")

spec = importlib.util.spec_from_file_location("attack_demo", ATTACK)
attack_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(attack_demo)

SOURCE = '''
local api_key = "sk_live_9f2a7c4e"
local endpoint = "https://api.example.com/v1/charge"
local function url(a)
  if a > 1000 then return endpoint .. "?key=" .. api_key end
  return endpoint .. "?amount=" .. a
end
print(url(500))
print(url(5000))
'''

LITERALS = ["sk_live_9f2a7c4e", "https://api.example.com/v1/charge"]


def leaks(build: str, mode: str) -> set[str]:
    found, _ok, _err = attack_demo.attack(build, mode)
    return {literal for literal in LITERALS if any(literal in text for text in found)}


class AttackDemoTests(unittest.TestCase):
    def test_medium_build_leaks_to_the_cheap_dumpers(self):
        build = obfuscate_source(SOURCE, preset("medium", seed=4)).code
        # this is exactly why 'medium' alone is not protection
        self.assertTrue(leaks(build, "concat"))

    def test_hardened_vm_defeats_the_cheap_dumpers(self):
        build = obfuscate_source(
            SOURCE, preset("nightmare", seed=4, anti_debug=False)
        ).code
        self.assertEqual(leaks(build, "concat"), set())
        self.assertEqual(leaks(build, "char"), set())

    def test_output_observation_always_leaks(self):
        # the honest ceiling: whatever the code stores, it must eventually use it
        for level in ("medium", "nightmare"):
            build = obfuscate_source(SOURCE, preset(level, seed=4, anti_debug=False)).code
            with self.subTest(level=level):
                self.assertTrue(leaks(build, "output"))

    def test_literal_extraction_helper(self):
        self.assertIn("sk_live_9f2a7c4e", attack_demo.lua_string_literals(SOURCE))


if __name__ == "__main__":
    unittest.main()
