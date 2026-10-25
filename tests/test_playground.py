import importlib.util
import os
import unittest

from luaobf.parser import parse

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYGROUND = os.path.join(os.path.dirname(HERE), "tools", "playground.py")

spec = importlib.util.spec_from_file_location("playground", PLAYGROUND)
playground = importlib.util.module_from_spec(spec)
spec.loader.exec_module(playground)


class PlaygroundApiTests(unittest.TestCase):
    def test_obfuscates(self):
        status, data = playground.obfuscate_request({"code": 'print("hi")', "level": "medium", "seed": 1})
        self.assertEqual(status, 200)
        self.assertIn("code", data)
        self.assertNotIn('"hi"', data["code"])
        parse(data["code"])
        self.assertEqual(data["stats"]["seed"], 1)
        self.assertNotIn("names", data["stats"])

    def test_syntax_error_is_a_400_with_position(self):
        status, data = playground.obfuscate_request({"code": "local = 1"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)
        self.assertEqual(data["line"], 1)

    def test_bad_level(self):
        status, data = playground.obfuscate_request({"code": "print(1)", "level": "nope"})
        self.assertEqual(status, 400)

    def test_bad_seed(self):
        status, data = playground.obfuscate_request({"code": "print(1)", "seed": "abc"})
        self.assertEqual(status, 400)

    def test_non_string_code(self):
        status, data = playground.obfuscate_request({"code": 42})
        self.assertEqual(status, 400)

    def test_verify_flag_present_in_response(self):
        status, data = playground.obfuscate_request({"code": "print(1)", "level": "light", "seed": 1})
        self.assertEqual(status, 200)
        self.assertIn("verified", data)
        self.assertFalse(data["verified"])

    def test_target_and_hardening_options(self):
        code = 'local n = 0\nfor i = 1, 5 do n = n + i end\nprint(n)\n'
        status, data = playground.obfuscate_request(
            {"code": code, "level": "nightmare", "target": "luau", "seed": 2,
             "anti_debug": True, "brand": "Forsakenis4"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["stats"]["target"], "luau")
        self.assertEqual(data["stats"]["string_mode"], "vm")
        self.assertNotIn("Forsakenis4", data["code"])

    def test_unknown_target(self):
        status, data = playground.obfuscate_request({"code": "print(1)", "target": "lua99"})
        self.assertEqual(status, 400)

    def test_lowering_error_is_reported_cleanly(self):
        status, data = playground.obfuscate_request(
            {"code": "::top:: goto top", "target": "lua51", "level": "light"}
        )
        self.assertEqual(status, 400)
        self.assertIn("goto", data["error"])

    def test_index_page_exists(self):
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(HERE), "playground", "index.html")))


if __name__ == "__main__":
    unittest.main()
