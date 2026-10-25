"""A per-string bytecode VM for string literals.

Every string gets its own tiny program: a list of 1-8 operations, each with its
own operand.  The bytes are stored *already transformed by the inverse program*,
so the runtime VM replays the program to recover the plaintext.

    payload := [op_count] { [opcode] [operand] } * op_count [cipher bytes…]

Opcodes (all pure arithmetic -- no bitwise operators, so this runs on Lua 5.1):

    1  x = (x + k) % 256
    2  x = (x * k) % 256            (k is odd, hence invertible mod 256)
    3  x = (x * 2) % 256 + floor(x / 128)     -- rotate left 1 bit
    4  x = floor(x / 2) + (x % 2) * 128       -- rotate right 1 bit

Why this is stronger than a single cipher: there is no fixed algorithm to
invert.  Two strings in the same build use different opcodes, different
operands and different lengths, so recognising "the" decoder and reversing it
once does not decode the others; a dumper has to actually run the VM.
"""

from __future__ import annotations

import base64
import random

#: opcode -> (forward, inverse).  Forward decodes cipher -> plaintext.
OPS = {
    1: (lambda x, k: (x + k) % 256, lambda x, k: (x - k) % 256),
    2: (lambda x, k: (x * k) % 256, lambda x, k: (x * pow(k, -1, 256)) % 256),
    3: (lambda x, k: (x * 2) % 256 + (x // 128), lambda x, k: (x // 2) + (x % 2) * 128),
    4: (lambda x, k: (x // 2) + (x % 2) * 128, lambda x, k: (x * 2) % 256 + (x // 128)),
}

MIN_OPS = 3
MAX_OPS = 7


def _random_ops(rng: random.Random, count: int) -> list[tuple[int, int]]:
    ops: list[tuple[int, int]] = []
    for _ in range(count):
        opcode = rng.choice((1, 1, 2, 2, 3, 4))
        if opcode == 2:
            operand = rng.choice([3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 251, 253])
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
    # undo the program, last operation first
    for opcode, operand in reversed(ops):
        inverse = OPS[opcode][1]
        cipher = [inverse(byte, operand) for byte in cipher]

    payload = bytes([count])
    for opcode, operand in ops:
        payload += bytes([opcode, operand])
    payload += bytes(cipher)
    return base64.b64encode(payload).decode("ascii")


def decode(payload: str) -> bytes:
    """Reference implementation used by the tests."""
    raw = base64.b64decode(payload)
    count = raw[0]
    ops = [(raw[1 + i * 2], raw[2 + i * 2]) for i in range(count)]
    data = list(raw[1 + count * 2 :])
    for opcode, operand in ops:
        forward = OPS[opcode][0]
        data = [forward(byte, operand) for byte in data]
    return bytes(data)


def runtime_source(names: dict[str, str]) -> str:
    """Lua implementation of the VM.

    ``names`` supplies the local names so the generated code blends into the
    rest of the obfuscated chunk (and gets renamed like everything else).
    """
    text = (
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
        "  local $data={}\n"
        "  for $i=1,#$raw-1-2*$n do $data[$i]=$raw[$i+1+2*$n] end\n"
        "  for $j=1,$n do\n"
        "    local $op=$raw[2*$j]\n"
        "    local $k=$raw[2*$j+1]\n"
        "    local $i=1\n"
        "    while $i<=#$data do\n"
        "      local $x=$data[$i]\n"
        "      if $op==1 then $x=($x+$k)%256\n"
        "      elseif $op==2 then $x=($x*$k)%256\n"
        "      elseif $op==3 then $x=($x*2)%256+math.floor($x/128)\n"
        "      elseif $op==4 then $x=math.floor($x/2)+($x%2)*128 end\n"
        "      $data[$i]=$x\n"
        "      $i=$i+1\n"
        "    end\n"
        "  end\n"
        "  local $chars={}\n"
        "  for $i=1,#$data do $chars[$i]=string.char($data[$i]) end\n"
        "  $hit=table.concat($chars)\n"
        "  $cache[$s]=$hit\n"
        "  return $hit\n"
        "end\n"
    )
    text = text.replace("ABC…", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
    # longest tokens first so no short name can clobber part of a longer one
    for token in sorted(names, key=len, reverse=True):
        text = text.replace("$" + token, names[token])
    return text


#: Local names the VM runtime needs.  ``dec`` must match the Encoder's decoder.
NAME_TOKENS = "alpha rev cache dec s hit raw buf bits i v pw n data j op k x chars".split()
