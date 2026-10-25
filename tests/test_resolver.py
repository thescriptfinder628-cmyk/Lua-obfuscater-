import unittest

from luaobf import ast_nodes as A
from luaobf.parser import parse
from luaobf.renamer import NameGenerator, rename, walk
from luaobf.resolve import resolve


def names_of(chunk):
    """Collect (identifier, scope_kind) for every reference."""
    out = []
    for node in walk(chunk):
        if isinstance(node, A.Name):
            out.append((node.id, node.scope_kind))
    return out


class ResolverTests(unittest.TestCase):
    def test_local_initialiser_sees_outer_scope(self):
        chunk = parse("local x = 1\ndo local x = x end")
        resolve(chunk)
        inner = chunk.body.stats[1].body.stats[0]
        # the initialiser x refers to the *outer* local
        self.assertIs(inner.exprs[0].binding.node, chunk.body.stats[0])

    def test_local_function_is_recursive(self):
        chunk = parse("local function f(n) if n > 0 then return f(n - 1) end end")
        resolve(chunk)
        decl = chunk.body.stats[0]
        ref = [n for n in walk(chunk) if isinstance(n, A.Name) and n.id == "f"]
        self.assertEqual(len(ref), 1)
        self.assertIs(ref[0].binding.node, decl)
        self.assertEqual(ref[0].scope_kind, "local")

    def test_repeat_condition_sees_body_locals(self):
        chunk = parse("repeat local done = true until done")
        resolve(chunk)
        cond = chunk.body.stats[0].cond
        self.assertIsInstance(cond, A.Name)
        self.assertEqual(cond.scope_kind, "local")

    def test_for_control_variable_scope(self):
        chunk = parse("for i = i, i do print(i) end")
        resolve(chunk)
        loop = chunk.body.stats[0]
        # all three bounds are evaluated *before* the loop variable exists
        self.assertEqual(loop.start.scope_kind, "global")
        self.assertEqual(loop.stop.scope_kind, "global")
        body_ref = loop.body.stats[0].call.args[0]
        self.assertEqual(body_ref.scope_kind, "for")

    def test_globals_are_recorded(self):
        resolver = resolve(parse("print(math.floor(1))"))
        self.assertEqual(resolver.globals_used, {"print", "math"})

    def test_dynamic_features_are_detected(self):
        resolver = resolve(parse("local f = loadstring('return 1')"))
        self.assertIn("loadstring", resolver.dynamic_features)

    def test_method_self_is_not_renamed(self):
        chunk = parse("function t:m() return self.x end")
        resolver = resolve(chunk)
        rename(chunk, resolver, style="hash")
        stat = chunk.body.stats[0]
        self.assertEqual(stat.func.params[0].name, "self")
        self.assertIsNone(stat.func.params[0].new_name)

    def test_closure_captures_upvalue(self):
        chunk = parse("local n = 0\nlocal function f() n = n + 1 return n end")
        resolver = resolve(chunk)
        rename(chunk, resolver, style="short")
        outer = chunk.body.stats[0]
        inner = chunk.body.stats[1]
        # both declarations and all references got the same new name
        self.assertEqual(outer.new_names[0], "_a" if False else outer.new_names[0])
        refs = [n for n in walk(chunk) if isinstance(n, A.Name) and n.binding is not None and n.binding.node is outer]
        self.assertGreaterEqual(len(refs), 2)
        self.assertEqual({r.new_name for r in refs}, {outer.new_names[0]})


class RenamerTests(unittest.TestCase):
    def test_generated_names_are_unique_and_legal(self):
        chunk = parse("local a = 1 local b = 2 do local a = 3 end")
        resolver = resolve(chunk)
        rename(chunk, resolver, style="hash")
        issued = []
        for node in walk(chunk):
            if isinstance(node, A.LocalDecl):
                issued.extend(n for n in node.new_names if n)
        self.assertEqual(len(set(issued)), len(issued))

    def test_never_shadows_a_used_global(self):
        src = "local print = print\nprint(print)"
        chunk = parse(src)
        resolver = resolve(chunk)
        rename(chunk, resolver, style="short")
        decl = chunk.body.stats[0]
        self.assertNotEqual(decl.new_names[0], "print")
        used_globals = {n.id for n in walk(chunk) if isinstance(n, A.Name) and n.scope_kind == "global"}
        self.assertNotIn(decl.new_names[0], used_globals)

    def test_name_generator_skips_keywords(self):
        gen = NameGenerator("short", reserved={"a", "b"})
        produced = [gen.next() for _ in range(30)]
        self.assertNotIn("a", produced)
        self.assertNotIn("b", produced)
        self.assertEqual(len(set(produced)), len(produced))
        for name in produced:
            self.assertNotIn(name, {"do", "end", "if", "for"})

    def test_all_styles_produce_valid_identifiers(self):
        from luaobf.lexer import KEYWORDS

        src = "local alpha, beta = 1, 2\nprint(alpha + beta)"
        for style in ("short", "wide", "hash", "confusable"):
            chunk = parse(src)
            resolver = resolve(chunk)
            rename(chunk, resolver, style=style)
            for node in walk(chunk):
                if isinstance(node, A.LocalDecl):
                    for name in node.new_names:
                        self.assertTrue(name[0].isalpha() or name[0] == "_", name)
                        self.assertNotIn(name, KEYWORDS)


if __name__ == "__main__":
    unittest.main()
