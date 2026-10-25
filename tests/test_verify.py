import unittest

from luaobf.obfuscator import obfuscate_source, preset
from luaobf.verify import VerificationUnavailable, backend, verify

HAS_LUA = backend() is not None


@unittest.skipUnless(HAS_LUA, "no Lua interpreter available")
class VerifyTests(unittest.TestCase):
    def test_matching_program_is_accepted(self):
        source = 'local t = {1,2,3}\nlocal s = 0\nfor i, v in ipairs(t) do s = s + v * i end\nprint(s)\n'
        code = obfuscate_source(source, preset("heavy", seed=2)).code
        ok, message = verify(source, code)
        self.assertTrue(ok, message)

    def test_changed_behaviour_is_rejected(self):
        source = 'print("a")\nprint("b")\n'
        code = source.replace('print("b")', 'print("c")')
        ok, message = verify(source, code)
        self.assertFalse(ok)
        self.assertIn("does not behave", message)

    def test_runtime_errors_are_compared(self):
        source = 'print("before")\nlocal x = nil\nprint(x.field)\n'
        code = obfuscate_source(source, preset("medium", seed=2)).code
        ok, message = verify(source, code)
        self.assertTrue(ok, message)

    def test_missing_interpreter_raises(self):
        import luaobf.verify as verify_module

        original_backend = verify_module.backend
        try:
            verify_module.backend = lambda: None
            with self.assertRaises(VerificationUnavailable):
                verify_module.verify("print(1)", "print(1)")
        finally:
            verify_module.backend = original_backend


if __name__ == "__main__":
    unittest.main()
