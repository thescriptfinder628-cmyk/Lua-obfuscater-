import unittest

from luaobf.lexer import EOF, KEYWORD, NAME, NUMBER, OP, STRING, LuaSyntaxError, tokenize


def kinds(src):
    return [(t.kind, t.value) for t in tokenize(src) if t.kind != EOF]


class LexerTests(unittest.TestCase):
    def test_keywords_and_names(self):
        self.assertEqual(
            kinds("local x = true"),
            [(KEYWORD, "local"), (NAME, "x"), (OP, "="), (KEYWORD, "true")],
        )

    def test_longest_operator_wins(self):
        self.assertEqual([v for k, v in kinds("a...b..c") if k == OP], ["...", ".."])
        self.assertEqual([v for k, v in kinds("a~=b<=c>>d") if k == OP], ["~=", "<=", ">>"])

    def test_numbers(self):
        self.assertEqual(kinds("1 2.5 1e3 0xFF 0x1p4 3."), [
            (NUMBER, (1, True)),
            (NUMBER, (2.5, False)),
            (NUMBER, (1000.0, False)),
            (NUMBER, (255, True)),
            (NUMBER, (16.0, False)),
            (NUMBER, (3.0, False)),
        ])

    def test_number_does_not_swallow_concat(self):
        tok = [t for t in tokenize("1..2")]
        self.assertEqual(tok[0].kind, NUMBER)
        self.assertEqual(tok[0].value, (1, True))
        self.assertEqual(tok[1].value, "..")
        self.assertEqual(tok[2].value, (2, True))

    def test_string_escapes(self):
        values = [t.value for t in tokenize(r'"a\nb\t\65\x41\u{48C}\z   end"') if t.kind == STRING]
        self.assertEqual(values, [b"a\nb\tAA\xd2\x8cend"])  # \u{48C} is U+048C

    def test_long_strings_and_comments(self):
        src = "[==[a]]b]==] --[[ block\ncomment ]] local x = 1"
        tokens = [t for t in tokenize(src)]
        self.assertEqual(tokens[0].kind, STRING)
        self.assertEqual(tokens[0].value, b"a]]b")
        self.assertEqual(tokens[1].value, "local")

    def test_long_string_skips_leading_newline(self):
        self.assertEqual([t.value for t in tokenize("[[\nhello]]") if t.kind == STRING], [b"hello"])

    def test_line_tracking(self):
        tokens = tokenize("a\nb\n  c")
        self.assertEqual([(t.value, t.line) for t in tokens if t.kind == NAME],
                         [("a", 1), ("b", 2), ("c", 3)])

    def test_errors(self):
        with self.assertRaises(LuaSyntaxError):
            tokenize('"unterminated')
        with self.assertRaises(LuaSyntaxError):
            tokenize("[[unterminated")
        with self.assertRaises(LuaSyntaxError):
            tokenize("0x")
        with self.assertRaises(LuaSyntaxError):
            tokenize("3abc")

    def test_bitwise_operators_lex(self):
        self.assertEqual([v for k, v in kinds("a & b | c ~ d << e >> f") if k == OP],
                         ["&", "|", "~", "<<", ">>"])


if __name__ == "__main__":
    unittest.main()
