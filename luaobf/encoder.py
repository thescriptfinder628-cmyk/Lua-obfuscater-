"""Literal obfuscation: encrypted strings and arithmetic-encoded numbers.

Strings
-------
Each string literal becomes ``<decoder>("<payload>")``.  The payload (before
Base64) is::

    [0]        index into the multiplier table
    [1]        key length (1..16)
    [2..2+k-1] key bytes
    [...]      cipher bytes, where  cipher = (byte * multiplier + key[i]) % 256

The multiplier is odd, hence invertible modulo 256, so decoding is exact.  The
decoder is emitted once at the top of the chunk, caches results, and uses only
arithmetic -- no bitwise operators -- so the output still runs on Lua 5.1.

Numbers
-------
Integers become ``(a+b)`` sums whose arithmetic is exact.  Floats are only
rewritten when the IEEE-754 round trip can be verified in Python first; anything
else is left as-is rather than risking a precision change.
"""

from __future__ import annotations

import base64
import math
import random

from . import ast_nodes as A
from . import stringvm
from .renamer import NameGenerator

#: Odd multipliers -- every one is invertible modulo 256.
MULTIPLIERS = [3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73]
KEY_LENGTHS = (3, 4, 5, 6, 7, 8, 11, 16)
B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

RUNTIME_TEMPLATE = r"""local $alpha="$alphabet"
local $rev={}
for $j=1,64 do $rev[($alpha):byte($j)]=$j-1 end
local $mul={$multiples}
local $cache={}
local function $dec($s)
  local $hit=$cache[$s]
  if $hit~=nil then return $hit end
  local $out,$buf,$bits={},0,0
  for $j=1,#$s do
    local $v=$rev[($s):byte($j)]
    if $v~=nil then
      $buf=$buf*64+$v
      $bits=$bits+6
      while $bits>7 do
        $bits=$bits-8
        local $p=2^$bits
        $out[#$out+1]=math.floor($buf/$p)%256
        $buf=$buf%$p
      end
    end
  end
  local $inv=$mul[$out[1]]
  local $klen=$out[2]
  local $key={}
  for $j=1,$klen do $key[$j]=$out[2+$j] end
  local $z={}
  for $j=1,#$out-2-$klen do
    $z[$j]=string.char((($out[$j+2+$klen]-$key[($j-1)%$klen+1])%256)*$inv%256)
  end
  $hit=table.concat($z)
  $cache[$s]=$hit
  return $hit
end
"""

_NAME_TOKENS = "alpha rev mul cache dec s hit out buf bits j v p inv klen key z".split()


def encode_bytes(data: bytes, key: bytes, mul: int) -> bytes:
    return bytes((b * mul + key[i % len(key)]) % 256 for i, b in enumerate(data))


def decode_bytes(cipher: bytes, key: bytes, mul: int) -> bytes:
    inv = pow(mul, -1, 256)
    return bytes((((c - key[i % len(key)]) % 256) * inv) % 256 for i, c in enumerate(cipher))


def encode_payload(data: bytes, rng: random.Random) -> str:
    mul_index = rng.randrange(len(MULTIPLIERS))
    mul = MULTIPLIERS[mul_index]
    key_len = rng.choice(KEY_LENGTHS)
    key = bytes(rng.randrange(1, 256) for _ in range(key_len))
    payload = bytes([mul_index, key_len]) + key + encode_bytes(data, key, mul)
    return base64.b64encode(payload).decode("ascii")


def runtime_source(names: dict[str, str]) -> str:
    """Render the Lua decoder using ``names`` for its locals."""
    multiples = ",".join(f"[{i}]={pow(m, -1, 256)}" for i, m in enumerate(MULTIPLIERS))
    text = RUNTIME_TEMPLATE
    text = text.replace("$alphabet", B64_ALPHABET).replace("$multiples", multiples)
    # longest tokens first so "$s" doesn't clobber "$s..." (no shared prefixes here,
    # but keep it safe)
    for tok in sorted(names, key=len, reverse=True):
        text = text.replace("$" + tok, names[tok])
    return text


class Encoder:
    def __init__(
        self,
        ngen: NameGenerator,
        rng: random.Random,
        encrypt_numbers: bool = True,
        encrypt_keys: bool = True,
        string_mode: str = "cipher",
    ) -> None:
        if string_mode not in ("cipher", "vm"):
            raise ValueError(f"unknown string mode {string_mode!r}")
        self.rng = rng
        self.ngen = ngen
        self.string_mode = string_mode
        self.encrypt_numbers = encrypt_numbers
        self.encrypt_keys = encrypt_keys
        #: every local name the generated runtime will use -- generated once so
        #: the decoder's own name matches the call sites we emit
        tokens = stringvm.NAME_TOKENS if string_mode == "vm" else _NAME_TOKENS
        self.names = {tok: ngen.next() for tok in tokens}
        self.decoder_name = self.names["dec"]
        self.uses_decoder = False
        self.strings_seen = 0

    def runtime(self) -> str:
        """Lua source of the decoder, or an empty string if it is not needed."""
        if not self.uses_decoder:
            return ""
        if self.string_mode == "vm":
            return stringvm.runtime_source(self.names)
        return runtime_source(self.names)

    # -- literal builders --------------------------------------------------

    def decoder_call(self, data: bytes) -> A.Expr:
        self.uses_decoder = True
        if self.string_mode == "vm":
            payload = stringvm.encode(data, self.rng).encode("ascii")
        else:
            payload = encode_payload(data, self.rng).encode("ascii")
        return A.Call(A.Name(self.decoder_name), [A.String(payload, None)])

    def encode_string(self, node: A.String, as_key: bool = False) -> A.Expr:
        if as_key and not self.encrypt_keys:
            return node
        if not node.value:
            return node  # "" is not worth hiding
        self.strings_seen += 1
        return self.decoder_call(node.value)

    def encode_number(self, node: A.Number) -> A.Expr:
        value = node.value
        if node.is_int:
            if abs(value) >= 2**52:
                return node
            offset = self.rng.randrange(1, 1 << 20) * self.rng.choice((-1, 1))
            other = value - offset
            return A.BinOp(
                "+",
                A.Number(offset, str(offset), True),
                A.Number(other, str(other), True),
            )

        if not math.isfinite(value):
            return node
        for _ in range(32):
            offset = self.rng.uniform(-1e6, 1e6)
            other = value - offset
            if math.isfinite(other) and offset + other == value:
                return A.BinOp(
                    "+",
                    A.Number(offset, repr(offset), False),
                    A.Number(other, repr(other), False),
                )
        return node

    # -- tree rewrite ------------------------------------------------------

    def transform(self, chunk: A.Chunk) -> None:
        self.rewrite(chunk)

    def rewrite(self, node: A.Node) -> A.Node:
        if node is None:
            return None
        if isinstance(node, A.String):
            return self.encode_string(node)
        if isinstance(node, A.Number):
            if self.encrypt_numbers:
                return self.encode_number(node)
            return node
        if isinstance(node, A.Index):
            node.obj = self.rewrite(node.obj)
            node.key = self._rewrite_key(node.key)
            return node
        if isinstance(node, A.TableField):
            node.key = self._rewrite_key(node.key) if node.key is not None else None
            node.value = self.rewrite(node.value)
            return node
        if isinstance(node, (A.FuncStat,)):
            # path/method stay plain identifiers (they are field accesses)
            node.func = self.rewrite(node.func)
            return node
        for field in getattr(node, "_fields", ()):
            value = getattr(node, field, None)
            if isinstance(value, list):
                setattr(node, field, [self._item(v) for v in value])
            elif isinstance(value, tuple):
                setattr(node, field, tuple(self._item(v) for v in value))
            elif isinstance(value, A.Node):
                setattr(node, field, self.rewrite(value))
        return node

    def _item(self, value):
        if isinstance(value, list):
            return [self._item(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._item(v) for v in value)
        if isinstance(value, A.Node):
            return self.rewrite(value)
        return value

    def _rewrite_key(self, node: A.Expr | None) -> A.Expr | None:
        if node is None:
            return None
        if isinstance(node, A.String):
            return self.encode_string(node, as_key=True)
        return self.rewrite(node)


def apply_encoder(chunk: A.Chunk, ngen: NameGenerator, rng: random.Random, **opts) -> tuple[str, Encoder]:
    """Run the encoding pass; returns the runtime prelude (may be empty)."""
    enc = Encoder(ngen, rng, **opts)
    enc.transform(chunk)
    prelude = enc.runtime() if enc.uses_decoder else ""
    return prelude, enc
