import unittest

import luaobf.ast_nodes as A
from luaobf.lexer import LuaSyntaxError
from luaobf.parser import parse
from luaobf.unparser import unparse


def roundtrip(src, minify=True):
    return unparse(parse(src), minify=minify)


class ParserTests(unittest.TestCase):
    def test_precedence_or(self):
        chunk = parse("x = 1 + 2 * 3 ^ 4")
        assign = chunk.body.stats[0]
        expr = assign.exprs[0]
        self.assertIsInstance(expr, A.BinOp)
        self.assertEqual(expr.op, "+")
        self.assertEqual(expr.right.op, "*")
        self.assertEqual(expr.right.right.op, "^")

    def test_concatenation_is_right_associative(self):
        expr = parse("x = 'a' .. 'b' .. 'c'").body.stats[0].exprs[0]
        self.assertEqual(expr.op, "..")
        self.assertEqual(expr.right.op, "..")

    def test_exponent_is_right_associative(self):
        expr = parse("x = 2 ^ 3 ^ 2").body.stats[0].exprs[0]
        self.assertEqual(expr.right.op, "^")

    def test_unary_binds_looser_than_power(self):
        # -2^2 parses as -(2^2)
        expr = parse("x = -2 ^ 2").body.stats[0].exprs[0]
        self.assertIsInstance(expr, A.UnOp)
        self.assertEqual(expr.operand.op, "^")

    def test_unary_not_binds_tighter_than_comparison(self):
        # Lua: (not a) == b
        expr = parse("x = not a == b").body.stats[0].exprs[0]
        self.assertIsInstance(expr, A.BinOp)
        self.assertEqual(expr.op, "==")
        self.assertIsInstance(expr.left, A.UnOp)
        self.assertEqual(expr.left.op, "not")

    def test_method_and_field_chain(self):
        call = parse("x = f().a.b:m(1)[2]").body.stats[0].exprs[0]
        self.assertIsInstance(call, A.Index)
        self.assertIsInstance(call.obj, A.MethodCall)
        self.assertEqual(call.obj.method, "m")

    def test_function_statement_sugar(self):
        stat = parse("function a.b.c:d(x) return x end").body.stats[0]
        self.assertIsInstance(stat, A.FuncStat)
        self.assertEqual(stat.path, ["a", "b", "c"])
        self.assertEqual(stat.method, "d")
        self.assertEqual([p.name for p in stat.func.params], ["self", "x"])

    def test_local_attributes(self):
        stat = parse("local x <const> = 1").body.stats[0]
        self.assertEqual(stat.attribs, ["const"])

    def test_table_fields(self):
        table = parse("t = {1, 2, x = 3, ['y'] = 4, [5] = 6,}").body.stats[0].exprs[0]
        self.assertEqual(len(table.fields), 5)
        self.assertIsNone(table.fields[0].key)
        self.assertEqual(table.fields[2].key.value, b"x")
        self.assertEqual(table.fields[4].key.value, 5)

    def test_varargs_and_calls(self):
        stat = parse("f(...)").body.stats[0]
        self.assertIsInstance(stat.call.args[0], A.Vararg)

    def test_string_and_table_call_sugar(self):
        a = parse("print 'hi'").body.stats[0].call
        b = parse("f{1}").body.stats[0].call
        self.assertIsInstance(a.args[0], A.String)
        self.assertIsInstance(b.args[0], A.Table)

    def test_numeric_for_with_step(self):
        stat = parse("for i = 1, 10, 2 do end").body.stats[0]
        self.assertIsInstance(stat, A.NumFor)
        self.assertIsNotNone(stat.step)

    def test_generic_for(self):
        stat = parse("for k, v in pairs(t) do end").body.stats[0]
        self.assertIsInstance(stat, A.GenFor)
        self.assertEqual(stat.vars, ["k", "v"])

    def test_labels_and_goto(self):
        stats = parse("::top:: goto top").body.stats
        self.assertIsInstance(stats[0], A.Label)
        self.assertIsInstance(stats[1], A.Goto)

    def test_return_must_end_block(self):
        with self.assertRaises(LuaSyntaxError):
            parse("return 1\nprint(2)")

    def test_syntax_errors_report_position(self):
        with self.assertRaises(LuaSyntaxError) as ctx:
            parse("local = 1", "test.lua")
        self.assertIn("test.lua:1:", str(ctx.exception))

    def test_empty_and_semicolon_statements(self):
        self.assertEqual(parse(";;;").body.stats, [])

    def test_roundtrip_is_stable(self):
        src = "local a = 1\nif a then print(a) end\n"
        once = roundtrip(src)
        twice = roundtrip(once)
        self.assertEqual(once, twice)

    def test_comments_are_dropped(self):
        self.assertNotIn("--", roundtrip("-- hello\nlocal a = 1 --[[x]]\n"))


class UnparserTests(unittest.TestCase):
    def check(self, src, expected=None):
        out = roundtrip(src)
        # the emitted code must parse back to an equivalent tree
        self.assertEqual(unparse(parse(out)), out)
        if expected is not None:
            self.assertEqual(out, expected)
        return out

    def test_negative_operand_spacing(self):
        # must not emit "a--b" (which would start a comment)
        out = self.check("x = a - -b")
        self.assertIn("- -", out)
        self.assertNotIn("--", out)

    def test_concat_with_number(self):
        out = self.check("x = 1 .. 2")
        self.assertNotIn("1..2", out)

    def test_keyword_identifier_boundaries(self):
        out = self.check("x = a and not b")
        self.assertIn("and not", out)
        # calling the result of a call is unambiguous: f()(1)
        out2 = self.check("x = f() (1)")
        self.assertEqual(out2, "x=f()(1)")

    def test_operator_gluing(self):
        for src in ["x = a < b", "x = a <= b", "x = 1 << 2", "x = a // b", "x = a ~= b"]:
            self.check(src)

    def test_precedence_is_preserved(self):
        cases = [
            "x = (1 + 2) * 3",
            "x = 1 + 2 * 3",
            "x = (a or b) and c",
            "x = a or (b and c)",
            "x = -(a + b)",
            "x = (a .. b) .. c",
            "x = a .. (b .. c)",
            "x = (a ^ b) ^ c",
            "x = 2 ^ (3 ^ 2)",
            "x = not (a and b)",
        ]
        for src in cases:
            first = unparse(parse(src))
            second = unparse(parse(first))
            self.assertEqual(first, second, src)

    def test_function_literal_used_as_callee_gets_parenthesised(self):
        out = self.check("x = (function() return 1 end)()")
        self.assertIn("(function()", out)

    def test_method_call_on_expression(self):
        out = roundtrip("x = (a + b):len()")
        self.assertIn("):", out)

    def test_pretty_print_indents(self):
        out = unparse(parse("if a then print(1) end"), minify=False)
        self.assertIn("\n  ", out)

    def test_string_quoting_is_safe(self):
        out = roundtrip('x = "a\\"b\\\\c\\n\\0"')
        self.assertIn('\\"', out)


if __name__ == "__main__":
    unittest.main()
