"""Wrap the whole program in an encrypted, self-loading bootstrap.

Static analysis is the cheapest way to attack an obfuscated file: open it, read
it.  The loader removes that option.  The protected file contains only:

* a decoder (the per-string VM),
* the program as a list of *encrypted segments*, each with its own opcode
  permutation and key,
* a checksum accumulated while the segments are handed to ``load``,
* integrity guards, and a call to ``load``.

There is no Lua logic in the file to read -- not even renamed logic.  To get the
program back an analyst must execute the file while hooking ``load``; and what
they then recover is the *next* layer, not the program.

Layout of one layer (names are random per build)::

    local <fail>  = function(m) error("<label>: " .. m, 0) end
    <integrity guards>                     -- optional (anti_debug)
    <decoder runtime>
    local <segs>  = { "<b64>", "<b64>", … }   -- the program, N encrypted segments
    local <acc>, <i> = 0, 1
    local function <reader>()                 -- decodes one segment per call
      …
    end
    local <chunk> = (loadstring or load)(<reader>, "@<name>")
    return <chunk>(...)

Why it is built this way:

1. **Segments, not a blob.**  The plaintext is never one string in memory: the
   reader decodes ~64 bytes at a time and ``load`` consumes them.  A dump taken
   at any single instant holds a fragment, not the program.
2. **A reader, not a string.**  ``load`` is given a function, so the classic
   one-line hook (``load = function(s) print(s) end``) prints ``function``.
3. **Guards before the load.**  If the environment's ``load``/``loadstring``/
   ``string.dump`` has been replaced by a Lua function, or a debug hook is
   installed, the build refuses to run at all.  A dumper has to be disarmed
   first, which means editing the file it cannot read.
4. **Layers.**  ``layers=N`` nests bootstraps: decrypting one yields another
   encrypted bootstrap.  Every layer repeats guards 1-3.

What this still does not defeat, stated plainly: the program must run, so a
debugger that single-steps the *decoder* can watch plaintext being produced, and
anything the program prints is visible.  ``tools/attack_demo.py`` measures
exactly that, so the claim stays honest.
"""

from __future__ import annotations

import random

from . import stringvm
from .renamer import NameGenerator

CHECKSUM_MOD = 2147483647

#: plaintext bytes handed to ``load`` per reader call
SEGMENT_SIZE = 64


def checksum(data: bytes) -> int:
    """``s = (s*31 + byte) % 2147483647`` -- identical in Python and Lua."""
    total = 0
    for byte in data:
        total = (total * 31 + byte) % CHECKSUM_MOD
    return total


def _guard(ngen: NameGenerator, fail: str) -> str:
    """Code that refuses to run under instrumentation.

    A hooked ``load``/``string.dump`` is a Lua closure; the real ones are C
    functions, and ``debug.getinfo`` can tell the difference.  Everything is
    optional: if ``debug`` is missing or restricted we simply cannot check.
    """
    dbg = ngen.next()
    getinfo = ngen.next()
    check = ngen.next()
    return (
        f'local {dbg}=rawget(_G,"debug")\n'
        f"local {getinfo}={dbg} and {dbg}.getinfo\n"
        f'if type({getinfo})=="function" then\n'
        f"  local function {check}(f)\n"
        f'    if type(f)~="function" then return end\n'
        f'    local ok,inf=pcall({getinfo},f,"S")\n'
        f'    if ok and type(inf)=="table" and inf.what~="C" then {fail}("integrity check failed") end\n'
        f"  end\n"
        f"  {check}(loadstring or load)\n"
        f"  {check}(string and string.dump)\n"
        f"  if type({dbg}.gethook)==\"function\" and {dbg}.gethook()~=nil then {fail}(\"integrity check failed\") end\n"
        f"end\n"
    )


def _layer(
    inner_code: str,
    ngen: NameGenerator,
    rng: random.Random,
    label: str,
    anti_debug: bool,
    chunk_name: str,
) -> str:
    raw = inner_code.encode("utf-8")
    pieces = []
    for start in range(0, len(raw), SEGMENT_SIZE):
        chunk = raw[start : start + SEGMENT_SIZE]
        pieces.append(stringvm.encode(chunk, rng))
    if not pieces:  # empty program: one empty segment keeps the reader simple
        pieces.append(stringvm.encode(b"", rng))

    decoder_names = {token: ngen.next() for token in stringvm.NAME_TOKENS}
    ns = {
        name: ngen.next()
        for name in (
            "fail",
            "dbg",
            "getinfo",
            "check",
            "segs",
            "acc",
            "idx",
            "reader",
            "piece",
            "seg",
            "k",
            "load",
            "probe",
            "ok",
            "chunk",
            "buf",
            "s",
            "name",
        )
    }

    guard = _guard(ngen, ns["fail"]) if anti_debug else ""
    table = ",".join(f'"{piece}"' for piece in pieces)
    total = checksum(raw)

    return (
        f'local {ns["fail"]}=function(m) error("{label}: "..tostring(m),0) end\n'
        + guard
        + stringvm.runtime_source(decoder_names)
        + f'local {ns["segs"]}={{{table}}}\n'
        f'local {ns["acc"]},{ns["idx"]}=0,1\n'
        f'local function {ns["reader"]}()\n'
        f'  local {ns["piece"]}={ns["segs"]}[{ns["idx"]}]\n'
        f'  if {ns["piece"]}==nil then\n'
        f'    if {ns["acc"]}~={total} then {ns["fail"]}("checksum mismatch") end\n'
        f"    return nil\n"
        f"  end\n"
        f'  {ns["idx"]}={ns["idx"]}+1\n'
        f'  local {ns["seg"]}={decoder_names["dec"]}({ns["piece"]})\n'
        f'  for {ns["k"]}=1,#{ns["seg"]} do '
        f'{ns["acc"]}=({ns["acc"]}*31+({ns["seg"]}):byte({ns["k"]}))%{CHECKSUM_MOD} end\n'
        f'  return {ns["seg"]}\n'
        f"end\n"
        f'local {ns["load"]}=loadstring or load\n'
        f'local {ns["ok"]},{ns["probe"]}=pcall({ns["load"]},function() return nil end)\n'
        f'local {ns["chunk"]}\n'
        f'if {ns["ok"]} and type({ns["probe"]})=="function" then\n'
        f'  {ns["chunk"]}={ns["load"]}({ns["reader"]},"@{chunk_name}")\n'
        f"else\n"
        f'  local {ns["buf"]}={{}}\n'
        f'  local {ns["s"]}={ns["reader"]}()\n'
        f'  while {ns["s"]}~=nil do {ns["buf"]}[#{ns["buf"]}+1]={ns["s"]} {ns["s"]}={ns["reader"]}() end\n'
        f'  {ns["chunk"]}={ns["load"]}(table.concat({ns["buf"]}),"@{chunk_name}")\n'
        f"end\n"
        f'if type({ns["chunk"]})~="function" then {ns["fail"]}("bootstrap failed") end\n'
        f"return {ns['chunk']}(...)\n"
    )


def build_loader(
    inner_code: str,
    ngen: NameGenerator,
    rng: random.Random,
    label: str = "protected chunk",
    anti_debug: bool = True,
    chunk_name: str = "protected",
    layers: int = 1,
) -> str:
    """Return a bootstrap that decrypts and runs ``inner_code``.

    ``layers`` nests further bootstraps: layer 1 decrypts to layer 2, which
    decrypts to ... and only the innermost layer is the program.
    """
    code = inner_code
    for _ in range(max(1, int(layers))):
        code = _layer(code, ngen, rng, label, anti_debug, chunk_name)
    return code
