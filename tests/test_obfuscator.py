"""End-to-end tests: obfuscate a corpus and compare real Lua behaviour."""

import glob
import os
import unittest

from luaobf.cli import main as cli_main
from luaobf.lexer import LuaSyntaxError
from luaobf.obfuscator import PRESETS, obfuscate_source, preset
from luaobf.parser import parse

try:
    from tests.lua_runner import HAVE_LUA, run_lua

    LUA_OK = HAVE_LUA
except Exception:  # pragma: no cover
    LUA_OK = False

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = sorted(glob.glob(os.path.join(HERE, "scripts", "*.lua")))


def load(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class CorpusReadable(unittest.TestCase):
    def test_corpus_files_parse(self):
        self.assertGreaterEqual(len(CORPUS), 6)
        for path in CORPUS:
            with self.subTest(path=os.path.basename(path)):
                parse(load(path))


@unittest.skipUnless(LUA_OK, "lupa not installed")
class DifferentialTests(unittest.TestCase):
    """The obfuscated program must behave exactly like the original."""

    def test_all_levels_match_original_behaviour(self):
        for path in CORPUS:
            source = load(path)
            baseline = run_lua(source)
            self.assertIsNone(baseline.error, f"{path} does not run: {baseline.error}")
            for level in PRESETS:
                for seed in (1, 7):
                    with self.subTest(file=os.path.basename(path), level=level, seed=seed):
                        result = obfuscate_source(source, preset(level, seed=seed))
                        actual = run_lua(result.code)
                        self.assertEqual(actual.output, baseline.output)
                        self.assertEqual(actual.error, baseline.error)

    def test_custom_option_matrix(self):
        source = load(os.path.join(HERE, "scripts", "basics.lua"))
        baseline = run_lua(source)
        variants = [
            dict(rename=False),
            dict(encrypt_strings=False),
            dict(encrypt_numbers=False),
            dict(encrypt_keys=False),
            dict(control_flow=False),
            dict(wrap=True),
            dict(minify=False),
            dict(rename_style="short", wrap=True, dead_code=2),
            dict(rename_style="confusable", encrypt_strings=True, junk_functions=3),
        ]
        for overrides in variants:
            opts = preset("medium", seed=99)
            for key, value in overrides.items():
                setattr(opts, key, value)
            with self.subTest(**{k: str(v) for k, v in overrides.items()}):
                result = obfuscate_source(source, opts)
                actual = run_lua(result.code)
                self.assertEqual(actual.output, baseline.output)
                self.assertEqual(actual.error, baseline.error)

    def test_error_behaviour_is_preserved(self):
        source = 'local function boom(x)\n  local y = x.field\n  return y\nend\nprint("before")\nboom(nil)\nprint("after")\n'
        baseline = run_lua(source)
        self.assertIsNotNone(baseline.error)
        result = obfuscate_source(source, preset("heavy", seed=3))
        actual = run_lua(result.code)
        self.assertEqual(actual.output, baseline.output)
        self.assertIsNotNone(actual.error)

    def test_unused_dead_code_never_runs(self):
        source = 'local x = 0\nfor i = 1, 100 do x = x + i end\nprint(x)\n'
        result = obfuscate_source(source, preset("insane", seed=5))
        baseline = run_lua(source)
        actual = run_lua(result.code)
        self.assertEqual(actual.output, baseline.output)


class StructureTests(unittest.TestCase):
    def test_output_parses_for_every_level(self):
        source = load(os.path.join(HERE, "scripts", "metatables.lua"))
        for level in PRESETS:
            with self.subTest(level=level):
                code = obfuscate_source(source, preset(level, seed=1)).code
                parse(code)

    def test_determinism_with_seed(self):
        source = load(os.path.join(HERE, "scripts", "closures.lua"))
        first = obfuscate_source(source, preset("heavy", seed=1234)).code
        second = obfuscate_source(source, preset("heavy", seed=1234)).code
        self.assertEqual(first, second)

    def test_different_seeds_differ(self):
        source = load(os.path.join(HERE, "scripts", "closures.lua"))
        a = obfuscate_source(source, preset("medium", seed=1)).code
        b = obfuscate_source(source, preset("medium", seed=2)).code
        self.assertNotEqual(a, b)

    def test_identifiers_are_gone(self):
        source = load(os.path.join(HERE, "scripts", "closures.lua"))
        code = obfuscate_source(source, preset("heavy", seed=1)).code
        for name in ("make_counter", "bump", "peek", "deepest", "level1", "level2"):
            self.assertNotIn(name, code)

    def test_string_literals_are_gone(self):
        source = 'local s = "super secret string"\nprint(s)\n'
        code = obfuscate_source(source, preset("medium", seed=1)).code
        self.assertNotIn("super secret string", code)

    def test_comments_are_removed(self):
        source = '-- a very revealing comment\nprint("x") -- trailing\n'
        code = obfuscate_source(source, preset("light", seed=1)).code
        self.assertNotIn("revealing", code)

    def test_light_level_shrinks_code(self):
        source = load(os.path.join(HERE, "scripts", "basics.lua"))
        result = obfuscate_source(source, preset("light", seed=1))
        self.assertLess(result.stats["output_bytes"], result.stats["input_bytes"])

    def test_shebang_is_preserved(self):
        source = "#!/usr/bin/env lua\nprint('hi')\n"
        code = obfuscate_source(source, preset("light", seed=1)).code
        self.assertTrue(code.startswith("#!/usr/bin/env lua\n"))

    def test_syntax_error_is_reported_with_location(self):
        with self.assertRaises(LuaSyntaxError) as ctx:
            obfuscate_source("local = 1", preset("light"))
        self.assertIn("1:", str(ctx.exception))

    def test_dynamic_globals_produce_a_warning(self):
        result = obfuscate_source("local f = loadstring('return 1')", preset("medium", seed=1))
        self.assertTrue(result.warnings)
        self.assertIn("loadstring", result.warnings[0])

    def test_strict_mode_raises_on_warning(self):
        opts = preset("medium", seed=1)
        opts.strict = True
        with self.assertRaises(RuntimeError):
            obfuscate_source("local f = loadstring('return 1')", opts)

    def test_empty_input(self):
        for level in PRESETS:
            self.assertEqual(obfuscate_source("", preset(level, seed=1)).code.strip(), "")

    def test_unicode_source(self):
        source = 'local s = "héllo ✓"\nprint(s)\n'
        code = obfuscate_source(source, preset("medium", seed=1)).code
        self.assertNotIn("héllo", code)

    def test_wrap_preserves_return_value(self):
        if not LUA_OK:
            self.skipTest("lupa not installed")
        source = "return 1 + 2\n"
        code = obfuscate_source(source, preset("heavy", seed=1)).code
        from lupa import LuaRuntime

        runtime = LuaRuntime()
        self.assertEqual(runtime.execute(code), 3)


class CliTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "in.lua")
        with open(self.src, "w", encoding="utf-8") as handle:
            handle.write('local msg = "hello"\nprint(msg)\n')

    def test_writes_output_file(self):
        out = os.path.join(self.tmp, "out.lua")
        code = cli_main([self.src, "-o", out, "--seed", "1"])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("hello", text)
        parse(text)

    def test_reports_syntax_errors(self):
        bad = os.path.join(self.tmp, "bad.lua")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("function f( end")
        self.assertEqual(cli_main([bad]), 2)

    def test_missing_file(self):
        self.assertEqual(cli_main([os.path.join(self.tmp, "nope.lua")]), 1)

    def test_map_file_is_written(self):
        out = os.path.join(self.tmp, "out.lua")
        mapping = os.path.join(self.tmp, "map.json")
        self.assertEqual(cli_main([self.src, "-o", out, "--map", mapping, "--seed", "2"]), 0)
        import json

        with open(mapping, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("msg", data)


if __name__ == "__main__":
    unittest.main()
