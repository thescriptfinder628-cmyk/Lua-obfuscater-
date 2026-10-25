"""A per-string bytecode VM for string literals.

Every string gets its own tiny program: a list of 3-8 operations, each with its
own operand.  The bytes are stored *already transformed by the inverse program*,
so the runtime VM replays the program to recover the plaintext.

    payload := [op_count][perm: 4 bytes]{[emitted opcode][operand]} * n[cipher…]

Three things make this harder to attack than a normal string decryptor:

1. **No fixed program.**  Opcodes, operands, count and order are random per
   string, so there is no single algorithm to reverse.

2. **No fixed dispatch.**  The opcode *numbers* are permuted per payload: the
   payload carries a 4-byte map, and the interpreter builds a table of closures
   from it.  There is no ``if op == 1 then …`` chain to pattern-match, and what
   opcode 3 means in one build means nothing in another.

3. **No ``string.char`` and mostly no ``table.concat``.**  Characters are cut
   out of an embedded byte-table with ``string.sub``, and short strings are
   assembled with ``..``.  The two hooks every Lua string dumper installs
   (``string.char`` and ``table.concat``) therefore see nothing useful.

Opcodes (pure arithmetic, no bitwise operators -> runs on Lua 5.1):

    1  x = (x + k) % 256
    2  x = (x * k) % 256                        (k is odd, invertible mod 256)
    3  x = (x * 2) % 256 + floor(x / 128)       -- rotate left 1 bit
    4  x = floor(x / 2) + (x % 2) * 128         -- rotate right 1 bit

What still defeats it: the plaintext has to exist in memory before the program
uses it.  ``tools/attack_demo.py --strategy memory`` shows exactly that.
"""

from __future__ import annotations

import base64
import random

#: canonical opcode -> (forward, inverse).  Forward decodes cipher -> plaintext.
OPS = {
    1: (lambda x, k: (x + k) % 256, lambda x, k: (x - k) % 256),
    2: (lambda x, k: (x * k) % 256, lambda x, k: (x * pow(k, -1, 256)) % 256),
    3: (lambda x, k: (x * 2) % 256 + (x // 128), lambda x, k: (x // 2) + (x % 2) * 128),
    4: (lambda x, k: (x // 2) + (x % 2) * 128, lambda x, k: (x * 2) % 256 + (x // 128)),
}

MIN_OPS = 3
MAX_OPS = 8

#: Strings at most this long are assembled with `..` instead of table.concat,
#: which keeps the plaintext away from the one hook that dumps everything.
CONCAT_THRESHOLD = 96

HEADER_SIZE = 5  # op count + 4-byte permutation


def _random_ops(rng: random.Random, count: int) -> list[tuple[int, int]]:
    ops: list[tuple[int, int]] = []
    for _ in range(count):
        opcode = rng.choice((1, 1, 2, 2, 3, 4))
        if opcode == 2:
            operand = rng.choice(
                [3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 251, 253]
            )
        elif opcode in (3, 4):
            operand = 0
        else:
            operand = rng.randrange(1, 256)
        ops.append((opcode, operand))
    return ops


def encode(data: bytes, rng: random.Random) -> str:
    """Encode ``data`` into a Base64 payload carrying its own decode program."""
    count = rng.randint(MIN_OPS, MAX_OPS)
    ops = _random_ops(rng, count)

    cipher = list(data)
    for opcode, operand in reversed(ops):
        inverse = OPS[opcode][1]
        cipher = [inverse(byte, operand) for byte in cipher]

    # per-payload opcode permutation: canonical op i is emitted as emitted[i]
    emitted = [1, 2, 3, 4]
    rng.shuffle(emitted)

    payload = bytes([count]) + bytes(emitted)
    for opcode, operand in ops:
        payload += bytes([emitted[opcode - 1], operand])
    payload += bytes(cipher)
    return base64.b64encode(payload).decode("ascii")


def decode(payload: str) -> bytes:
    """Reference implementation (used by the tests)."""
    raw = base64.b64decode(payload)
    count = raw[0]
    emitted = list(raw[1:5])
    # emitted[i-1] is the id used for canonical opcode i
    canonical = {eid: index + 1 for index, eid in enumerate(emitted)}

    ops = [(canonical[raw[2 * j + 3]], raw[2 * j + 4]) for j in range(1, count + 1)]
    data = list(raw[HEADER_SIZE + 2 * count :])
    for opcode, operand in ops:
        forward = OPS[opcode][0]
        data = [forward(byte, operand) for byte in data]
    return bytes(data)


def _byte_literal() -> str:
    """A Lua string literal containing every byte value 0…255."""
    from .unparser import quote_string

    return quote_string(bytes(range(256)))


def runtime_source(names: dict[str, str]) -> str:
    """Lua implementation of the VM, using the caller's local names."""
    text = (
        'local $bytes=' + _byte_literal() + "\n"
        'local $alpha="ABC…"\n'
        "local $rev={}\n"
        "for $i=1,64 do $rev[($alpha):byte($i)]=$i-1 end\n"
        "local $cache={}\n"
        "local function $dec($s)\n"
        "  local $hit=$cache[$s]\n"
        "  if $hit~=nil then return $hit end\n"
        "  local $raw,$buf,$bits={},0,0\n"
        "  for $i=1,#$s do\n"
        "    local $v=$rev[($s):byte($i)]\n"
        "    if $v~=nil then\n"
        "      $buf=$buf*64+$v\n"
        "      $bits=$bits+6\n"
        "      while $bits>7 do\n"
        "        $bits=$bits-8\n"
        "        local $pw=2^$bits\n"
        "        $raw[#$raw+1]=math.floor($buf/$pw)%256\n"
        "        $buf=$buf%$pw\n"
        "      end\n"
        "    end\n"
        "  end\n"
        "  local $n=$raw[1]\n"
        "  local $perm={}\n"
        "  for $i=1,4 do $perm[$i]=$raw[1+$i] end\n"
        "  local $sem={\n"
        "    function($x,$k) return ($x+$k)%256 end,\n"
        "    function($x,$k) return ($x*$k)%256 end,\n"
        "    function($x,$k) return ($x*2)%256+math.floor($x/128) end,\n"
        "    function($x,$k) return math.floor($x/2)+($x%2)*128 end,\n"
        "  }\n"
        "  local $fns={}\n"
        "  for $i=1,4 do $fns[$perm[$i]]=$sem[$i] end\n"
        "  local $data={}\n"
        "  local $off=5+2*$n\n"
        "  for $i=1,#$raw-$off do $data[$i]=$raw[$off+$i] end\n"
        "  for $j=1,$n do\n"
        "    local $fn=$fns[$raw[4+2*$j]]\n"
        "    local $k=$raw[5+2*$j]\n"
        "    for $i=1,#$data do $data[$i]=$fn($data[$i],$k) end\n"
        "  end\n"
        "  local $out,$chars='',{}\n"
        "  if #$data<=" + str(CONCAT_THRESHOLD) + " then\n"
        "    for $i=1,#$data do $out=$out..($bytes):sub($data[$i]+1,$data[$i]+1) end\n"
        "  else\n"
        "    for $i=1,#$data do $chars[$i]=($bytes):sub($data[$i]+1,$data[$i]+1) end\n"
        "    $out=table.concat($chars)\n"
        "  end\n"
        "  $cache[$s]=$out\n"
        "  return $out\n"
        "end\n"
    )
    text = text.replace(
        "ABC…", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    )
    for token in sorted(names, key=len, reverse=True):
        text = text.replace("$" + token, names[token])
    return text


NAME_TOKENS = "alpha rev cache dec s hit raw buf bits i v pw n perm sem fns data j fn k x out chars bytes off".split()
