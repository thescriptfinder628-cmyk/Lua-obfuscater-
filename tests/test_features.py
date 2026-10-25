"""Tests for the multi-dialect and hardening features."""

import glob
import os
import random
import unittest

from luaobf import stringvm
from luaobf.flatten import apply_flatten
from luaobf.lower import LoweringError, lower_chunk
from luaobf.obfuscator import PRESETS, obfuscate_source, preset
from luaobf.parser import parse
from luaobf.renamer import NameGenerator
from luaobf.resolve import resolve
from luaobf.targets import TARGETS, detect_dialect
from luaobf.unparser import unparse

try:
    from tests.lua_runner import HAVE_LUA, run_lua
except Exception:  # pragma: no cover
    HAVE_LUA = False

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = sorted(glob.glob(os.path.join(HERE, "scripts", "*.lua")))

LUAU_SOURCE = """
--!strict
type Point = { x: number, y: number }
local p: Point = { x = 1, y = 2 }
local n: number = 0
for i = 1, 10 do
  if i % 2 == 0 then continue end
  n += i
end
local t = { count = 0 }
t.count += n
local label = if n > 20 then "big" else "small"
print(label, n, t.count, p.x)
"""

LUAU_EXPECTED = ["small\t25\t25\t1"]


class LuauParsingTests(unittest.TestCase):
    def test_types_and_annotations_are_erased(self):
        chunk = parse(LUAU_SOURCE, "t.luau")
        code = unparse(chunk)
        for word in ("type Point", ": number", "<T>", "->"):
            self.assertNotIn(word, code)
        self.assertIn("continue", code)  # kept for the Luau dialect

    def test_compound_assignment_expands(self):
        chunk = parse("local x = 1 x += 2 x *= 3 local s = 'a' s ..= 'b'", "t.luau")
        code = unparse(chunk)
        self.assertNotIn("+=", code)
        self.assertNotIn("..=", code)
        self.assertIn("x=x+2", code)

    def test_compound_assignment_evaluates_receiver_once(self):
        chunk = parse("holder.fetch().count += 1", "t.luau")
        code = unparse(chunk)
        # the receiver expression must be hoisted into a temporary, not repeated
        self.assertEqual(code.count("fetch()"), 1)
        self.assertIn("do", code)

    def test_if_expression(self):
        chunk = parse("local x = if a then 1 elseif b then 2 else 3", "t.luau")
        self.assertIn("elseif", unparse(chunk))

    def test_dialect_detection(self):
        self.assertEqual(detect_dialect("local x: number = 1"), "luau")
        self.assertEqual(detect_dialect("--!strict\nlocal x = 1"), "luau")
        self.assertEqual(detect_dialect("local x = 1"), "lua")

    def test_strict_lua_rejects_luau(self):
        with self.assertRaises(Exception):
            parse("local x: number = 1", "t.lua", dialect="lua")
        # ... while auto-detection accepts it
        self.assertEqual(parse("local x: number = 1", "t.luau").dialect, "luau")


class TargetTests(unittest.TestCase):
    def _lower(self, source, target, dialect="auto"):
        chunk = parse(source, "t", dialect=dialect)
        prelude, _warnings = lower_chunk(chunk, TARGETS[target])
        return (prelude + "\n" if prelude else "") + unparse(chunk)

    def test_continue_lowering_lua52_uses_goto(self):
        code = self._lower("for i=1,3 do if i==2 then continue end print(i) end", "lua52")
        self.assertIn("goto", code)
        self.assertNotIn("continue", code)

    def test_continue_lowering_lua51_uses_repeat_break(self):
        code = self._lower("for i=1,3 do if i==2 then continue end print(i) end", "lua51")
        self.assertNotIn("goto", code)
        self.assertNotIn("continue", code)
        self.assertIn("repeat", code)

    def test_lua51_rejects_goto(self):
        with self.assertRaises(LoweringError):
            self._lower("::top:: goto top", "lua51")

    def test_luau_keeps_continue(self):
        code = self._lower("for i=1,3 do if i==2 then continue end end", "luau")
        self.assertIn("continue", code)

    def test_if_expression_lowered_for_plain_lua(self):
        code = self._lower("local x = if a then 1 else 2", "lua54")
        self.assertNotIn("if a then 1 else 2", code)
        self.assertTrue("and" in code or "function" in code)

    def test_bitwise_emulation_for_lua52(self):
        code = self._lower("print(1 & 3)", "lua52")
        self.assertNotIn("&", code)
        self.assertIn("local function", code)

    def test_floor_division_lowered_for_lua51(self):
        code = self._lower("print(7 // 2)", "lua51")
        self.assertNotIn("//", code)
        self.assertIn("math.floor", code)

    def test_attributes_stripped_for_old_targets(self):
        code = self._lower("local x <const> = 1", "lua51")
        self.assertNotIn("<const>", code)

    @unittest.skipUnless(HAVE_LUA, "lupa not installed")
    def test_lowering_preserves_behaviour(self):
        source = """
        local total = 0
        for i = 1, 10 do
          if i % 2 == 0 then continue end
          if i > 7 then break end
          total = total + i
        end
        print(total)
        local n = 0
        while n < 6 do
          n = n + 1
          if n % 3 == 0 then continue end
          total = total + n
        end
        print(total)
        """
        # compare the lowered variants against each other
        lua51 = run_lua(self._lower(source, "lua51", dialect="luau"))
        lua52 = run_lua(self._lower(source, "lua52", dialect="luau"))
        self.assertEqual(lua51.output, lua52.output)
        self.assertIsNone(lua51.error)


class FlattenTests(unittest.TestCase):
    def _flatten(self, source, seed=1):
        chunk = parse(source)
        resolver = resolve(chunk)
        ngen = NameGenerator("hash", set(resolver.globals_used), seed)
        count = apply_flatten(chunk, random.Random(seed), ngen, resolver)
        return count, unparse(chunk)

    def test_straight_line_code_becomes_a_state_machine(self):
        # no local crosses a statement boundary here, so it can be split freely
        source = 'print("a")\nprint("b")\nprint("c")\n'
        count, code = self._flatten(source)
        self.assertGreaterEqual(count, 1)
        self.assertIn("while true do", code)
        self.assertNotIn('print("a") print("b")', code)

    def test_interdependent_statements_are_kept_together(self):
        # `a` is used by every later statement: no legal cut points exist
        source = "local a = 1\nprint(a)\na = a + 1\nprint(a)\n"
        count, _code = self._flatten(source)
        self.assertEqual(count, 0)

    def test_locals_still_scope_correctly(self):
        # `b` is used later, so the two statements must share a segment
        source = "local a = 1\nlocal b = a + 1\nprint(b)\nprint(a)\n"
        _count, code = self._flatten(source)
        parse(code)  # must still be valid Lua
        if HAVE_LUA:
            self.assertEqual(run_lua(code).error, None)

    def test_loops_with_jumps_are_left_alone(self):
        source = (
            "local t = 0\n"
            "for i = 1, 3 do\n"
            "  if i == 2 then break end\n"
            "  t = t + i\n"
            "end\n"
            "print(t)\n"
        )
        _count, code = self._flatten(source)
        self.assertIn("for i=1,3", code)

    def test_repeat_bodies_are_not_flattened(self):
        source = "local n = 0\nrepeat local done = n >= 2 n = n + 1 until done\nprint(n)\n"
        _count, code = self._flatten(source)
        self.assertIn("until", code)

    @unittest.skipUnless(HAVE_LUA, "lupa not installed")
    def test_behaviour_is_preserved_on_corpus(self):
        for path in CORPUS:
            source = open(path).read()
            baseline = run_lua(source)
            for seed in (1, 2, 3):
                with self.subTest(file=os.path.basename(path), seed=seed):
                    _count, code = self._flatten(source, seed)
                    actual = run_lua(code)
                    self.assertEqual(actual.output, baseline.output)


class StringVmTests(unittest.TestCase):
    def test_python_roundtrip(self):
        rng = random.Random(3)
        for data in (b"", b"x", b"\x00\xff\x80 binary", "héllo ✓".encode(), b"q" * 400):
            self.assertEqual(stringvm.decode(stringvm.encode(data, rng)), data)

    def test_each_string_gets_a_different_program(self):
        rng = random.Random(9)
        payloads = {stringvm.encode(b"same", rng) for _ in range(20)}
        self.assertGreater(len(payloads), 15)

    def test_runtime_uses_no_bitwise_operators(self):
        names = {t: "_v" + str(i) for i, t in enumerate(stringvm.NAME_TOKENS)}
        source = stringvm.runtime_source(names)
        # `~=` is inequality and `>` ends `-->` / comparisons; check the *tokens*
        from luaobf.lexer import OP, tokenize

        ops = {tok.value for tok in tokenize(source) if tok.kind == OP}
        for op in ("&", "|", "~", "<<", ">>"):
            self.assertNotIn(op, ops)
        self.assertNotIn("$", source)

    @unittest.skipUnless(HAVE_LUA, "lupa not installed")
    def test_vm_strings_execute_in_lua(self):
        rng = random.Random(4)
        samples = [b"hello", b"", b"\x00\xff", "ünïcode".encode(), b"z" * 50]
        payloads = [stringvm.encode(s, rng) for s in samples]
        names = {t: "_v" + str(i) for i, t in enumerate(stringvm.NAME_TOKENS)}
        script = stringvm.runtime_source(names)
        script += "\nlocal R={}\n"
        for payload in payloads:
            script += f'R[#R+1]={names["dec"]}("{payload}")\n'
        script += "local o={} for i=1,#R do o[i]=#R[i] end print(table.concat(o,','))\n"
        result = run_lua(script)
        self.assertIsNone(result.error)
        self.assertEqual(result.output, [",".join(str(len(s)) for s in samples)])


@unittest.skipUnless(HAVE_LUA, "lupa not installed")
class EmulationArtifactTests(unittest.TestCase):
    """Documented limitation: emulated bitwise ops yield floats.

    On targets without an integer subtype (5.1/5.2/LuaJIT) every number is a
    float, so ``1 << 4`` *is* ``16.0`` there and prints as ``16``.  Our test
    interpreter is Lua 5.5, which has integers, so the emulated build prints
    ``16.0`` where the original printed ``16``.  The values are equal; only the
    formatting differs.  This test pins that down so a *real* regression (a
    changed value) still fails loudly.
    """

    def test_bitwise_emulation_only_changes_float_formatting(self):
        import re

        source = "print(1 << 4, 255 >> 4, 12 & 10, 12 | 3, 12 ~ 10)\n"
        baseline = run_lua(source)
        build = run_lua(obfuscate_source(source, preset("light", seed=1, target="lua52")).code)
        strip = lambda lines: [re.sub(r"(?<=\d)\.0(?=\D|$)", "", line) for line in lines]
        self.assertEqual(strip(build.output), strip(baseline.output))
        self.assertEqual(
            [float(v) for v in build.output[0].split("\t")],
            [float(v) for v in baseline.output[0].split("\t")],
        )


class HardeningTests(unittest.TestCase):
    def test_nightmare_level_enables_everything(self):
        source = open(os.path.join(HERE, "scripts", "realworld.lua")).read()
        result = obfuscate_source(source, preset("nightmare", seed=1))
        self.assertEqual(result.stats["string_mode"], "vm")
        self.assertGreater(result.stats["flattened"], 0)
        self.assertIn("integrity", result.code.lower())

    def test_brand_watermark_is_hidden(self):
        source = 'print("hello")\n'
        result = obfuscate_source(source, preset("insane", seed=1, brand="Forsakenis4"))
        self.assertNotIn("Forsakenis4", result.code)

    def test_target_is_reported(self):
        source = "print(1)\n"
        self.assertEqual(obfuscate_source(source, preset("light", seed=1)).stats["target"], "lua51")
        self.assertEqual(
            obfuscate_source(source, preset("light", seed=1, target="luau")).stats["target"], "luau"
        )

    def test_all_levels_emit_valid_code_for_every_target(self):
        source = open(os.path.join(HERE, "scripts", "basics.lua")).read()
        for level in PRESETS:
            for target in ("lua51", "lua52", "lua53", "lua54", "luajit", "luau"):
                with self.subTest(level=level, target=target):
                    code = obfuscate_source(source, preset(level, seed=2, target=target)).code
                    parse(code, dialect=TARGETS[target].dialect)


if __name__ == "__main__":
    unittest.main()
