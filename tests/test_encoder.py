import base64
import math
import random
import unittest

from luaobf import ast_nodes as A
from luaobf.encoder import MULTIPLIERS, Encoder, decode_bytes, encode_payload, runtime_source
from luaobf.renamer import NameGenerator

try:
    from tests.lua_runner import run_lua

    HAVE_LUA = True
except Exception:  # pragma: no cover
    HAVE_LUA = False


class PayloadTests(unittest.TestCase):
    def test_roundtrip_many_strings(self):
        rng = random.Random(1234)
        samples = [
            b"",
            b"a",
            b"print",
            b"hello world",
            bytes(range(256)),
            "héllo wörld ✓".encode(),
            b"x" * 5000,
        ]
        for data in samples:
            payload = base64.b64decode(encode_payload(data, rng))
            mul_index = payload[0]
            key_len = payload[1]
            key = payload[2 : 2 + key_len]
            cipher = payload[2 + key_len :]
            mul = MULTIPLIERS[mul_index]
            self.assertEqual(decode_bytes(cipher, key, mul), data)

    def test_every_multiplier_is_invertible(self):
        for mul in MULTIPLIERS:
            self.assertEqual(mul % 2, 1)
            inv = pow(mul, -1, 256)
            self.assertEqual((mul * inv) % 256, 1)

    def test_payloads_are_not_the_plaintext(self):
        rng = random.Random(9)
        data = b"SECRET_API_KEY"
        payload = encode_payload(data, rng)
        self.assertNotIn("SECRET", payload)
        self.assertNotIn(base64.b64encode(data).decode()[:8], payload)

    def test_same_string_encodes_differently_each_time(self):
        rng = random.Random(5)
        outputs = {encode_payload(b"same", rng) for _ in range(20)}
        self.assertGreater(len(outputs), 15)

    def test_runtime_has_no_placeholders(self):
        src = runtime_source({k: "_v" + str(i) for i, k in enumerate("alpha rev mul cache dec s hit out buf bits j v p inv klen key z".split())})
        self.assertNotIn("$", src.replace("$", "", 0))


class NumberTests(unittest.TestCase):
    def test_integer_encoding_is_exact(self):
        rng = random.Random(3)
        enc = Encoder(NameGenerator("hash", set(), 0), rng)
        for value in [0, 1, -1, 42, 1000000, -(2**40), 2**52 - 1]:
            node = A.Number(value, str(value), True)
            out = enc.encode_number(node)
            if out is node:
                continue
            self.assertIsInstance(out, A.BinOp)
            result = out.left.value + out.right.value
            self.assertEqual(result, value)

    def test_float_encoding_is_exact(self):
        rng = random.Random(4)
        enc = Encoder(NameGenerator("hash", set(), 0), rng)
        for value in [0.5, -2.5, 3.141592653589793, 1e-9, 1234.5678]:
            node = A.Number(value, repr(value), False)
            out = enc.encode_number(node)
            if out is node:
                continue
            self.assertEqual(out.left.value + out.right.value, value)

    def test_huge_integers_are_left_alone(self):
        rng = random.Random(5)
        enc = Encoder(NameGenerator("hash", set(), 0), rng)
        node = A.Number(2**60, str(2**60), True)
        self.assertIs(enc.encode_number(node), node)

    def test_non_finite_is_left_alone(self):
        rng = random.Random(6)
        enc = Encoder(NameGenerator("hash", set(), 0), rng)
        for value in (math.inf, -math.inf, math.nan):
            node = A.Number(value, "0/0", False)
            self.assertIs(enc.encode_number(node), node)


@unittest.skipUnless(HAVE_LUA, "lupa not installed")
class DecoderRuntimeTests(unittest.TestCase):
    def test_decoder_roundtrip_in_lua(self):
        rng = random.Random(11)
        samples = [b"hello", b"", b"\x00\x01\xff", "ünïcödé".encode(), b"z" * 200]
        payloads = [encode_payload(s, rng) for s in samples]

        gen = NameGenerator("hash", set(), 0)
        enc = Encoder(gen, rng)
        runtime = enc.runtime() if False else None
        enc.uses_decoder = True
        runtime = enc.runtime()
        dec = enc.decoder_name

        script = runtime + "\nlocal R={}\n"
        for p in payloads:
            script += f'R[#R+1]={dec}("{p}")\n'
        script += 'for i=1,#R do local s=R[i] local o={} for j=1,#s do o[#o+1]=string.byte(s,j) end print(#s..":"..table.concat(o,",")) end\n'

        result = run_lua(script)
        self.assertIsNone(result.error)
        expected = [f"{len(s)}:" + ",".join(str(b) for b in s) for s in samples]
        self.assertEqual(result.output, expected)

    def test_decoder_caches_and_is_idempotent(self):
        rng = random.Random(12)
        gen = NameGenerator("hash", set(), 0)
        enc = Encoder(gen, rng)
        enc.uses_decoder = True
        payload = encode_payload(b"cache me", rng)
        script = enc.runtime() + f'\nlocal a={enc.decoder_name}("{payload}")\nlocal b={enc.decoder_name}("{payload}")\nprint(a==b, a)\n'
        result = run_lua(script)
        self.assertIsNone(result.error)
        self.assertEqual(result.output, ["true\tcache me"])


if __name__ == "__main__":
    unittest.main()
