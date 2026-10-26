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
    <integrity guards>                        -- optional (anti_debug)
    <decoder runtime>
    local <segs>  = { "<b64>", "<b64>", … }   -- the program, N encrypted segments
    local <acc>, <i> = 0, 1
    local function <reader>()                  -- decodes one segment per call
      …
    end
    local <chunk> = (loadstring or load)(<reader>, "@<name>")
    return <chunk>(...)

Four separate things make this different from "encrypt the source and ``load`` it":

1. **Segments, not a blob.**  The plaintext is never one string in memory: the
   reader decodes 64 bytes at a time and ``load`` consumes them.
2. **A reader, not a string.**  ``load`` is given a function, so the classic
   one-line hook (``load = function(s) print(s) end``) prints ``function``.
3. **Guards before the load.**  If the environment's ``load``/``loadstring``/
   ``string.dump`` has been replaced by a Lua function, or a debug hook is
   installed, the build refuses to run at all.
4. **Layers.**  ``layers=N`` nests bootstraps: decrypting one yields another
   encrypted bootstrap.  Every layer repeats guards 1-3.

``self_check=True`` adds a fifth: the file hashes **itself** before decoding
anything and passes a token to the layer below, so deleting the guards is as
fatal as editing them.  The hash literal sits outside the hashed region (it
cannot contain its own digest), which is exactly why it is not a signature --
an attacker who edits the body can recompute it.  It raises the cost of the
"delete the anti-debug lines" trick from a two-second edit to a rewrite.

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
        f'    if ok and type(inf)=="table" and inf.what~="C" then '
        f'{fail}("integrity check failed") end\n'
        f"  end\n"
        f"  {check}(loadstring or load)\n"
        f"  {check}(string and string.dump)\n"
        f'  if type({dbg}.gethook)=="function" and {dbg}.gethook()~=nil then '
        f'{fail}("integrity check failed") end\n'
        f"end\n"
    )


def _layer(
    inner_code: str,
    ngen: NameGenerator,
    rng: random.Random,
    label: str,
    anti_debug: bool,
    chunk_name: str,
    gate: tuple[str, str] | None = None,
) -> str:
    raw = inner_code.encode("utf-8")
    pieces = []
    for start in range(0, len(raw), SEGMENT_SIZE):
        pieces.append(stringvm.encode(raw[start : start + SEGMENT_SIZE], rng))
    if not pieces:  # empty program: keep the reader uniform
        pieces.append(stringvm.encode(b"", rng))

    decoder_names = {token: ngen.next() for token in stringvm.NAME_TOKENS}
    ns = {
        name: ngen.next()
        for name in (
            "fail",
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
        )
    }

    guard = ""
    if gate:
        # the file-level self check must have succeeded before this layer is
        # allowed to decode anything, so deleting that section is fatal too
        guard = f'if {gate[0]}~={gate[1]} then {ns["fail"]}("integrity check failed") end\n'
    if anti_debug:
        guard += _guard(ngen, ns["fail"])

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
        f'  while {ns["s"]}~=nil do {ns["buf"]}[#{ns["buf"]}+1]={ns["s"]} '
        f'{ns["s"]}={ns["reader"]}() end\n'
        f'  {ns["chunk"]}={ns["load"]}(table.concat({ns["buf"]}),"@{chunk_name}")\n'
        f"end\n"
        f'if type({ns["chunk"]})~="function" then {ns["fail"]}("bootstrap failed") end\n'
        f"return {ns['chunk']}(...)\n"
    )


def _self_check_head(ngen: NameGenerator, rng: random.Random, label: str) -> tuple[str, str, str, str]:
    """Head of the self-checking wrapper.

    Returns ``(head, gate_name, gate_value, tokens)`` where ``tokens`` is the
    ``(start_marker, end_marker)`` pair.  ``head`` runs before anything is
    decoded: it reads its own file, hashes the marked region (the guards and the
    payload) and sets the gate only if the checksum matches.  The checksum
    literal lives *before* the start marker, so it is not part of its own
    input.
    """
    letters = "abcdefghijklmnopqrstuvwxyz"
    start_token = "".join(rng.choice(letters) for _ in range(12))
    end_token = "".join(rng.choice(letters) for _ in range(12))
    # split so neither marker appears literally inside the checking code
    s1, s2 = start_token[:6], start_token[6:]
    e1, e2 = end_token[:6], end_token[6:]

    ns = {
        name: ngen.next()
        for name in (
            "fail",
            "gate",
            "sum",
            "io",
            "dbg",
            "getinfo",
            "ok",
            "info",
            "src",
            "handle",
            "body",
            "from",
            "to",
            "region",
            "acc",
            "i",
            "byte",
        )
    }
    gate_value = str(rng.randint(100000000, 999999999))
    head = (
        f'local {ns["fail"]}=function(m) error("{label}: "..tostring(m),0) end\n'
        f'local {ns["gate"]}=nil\n'
        f'local {ns["sum"]}="__SELF_SUM__"\n'
        f'local {ns["io"]},{ns["dbg"]}=rawget(_G,"io"),rawget(_G,"debug")\n'
        f'local {ns["getinfo"]}={ns["dbg"]} and {ns["dbg"]}.getinfo\n'
        f'if {ns["io"]} and {ns["getinfo"]} then\n'
        # level 1 inside the closure is the closure itself, so the source is
        # this file no matter how many pcall frames sit above it
        f'  local {ns["ok"]},{ns["info"]}=pcall(function() return {ns["getinfo"]}(1,"S") end)\n'
        f'  local {ns["src"]}={ns["ok"]} and {ns["info"]} and {ns["info"]}.source\n'
        f'  if type({ns["src"]})=="string" and {ns["src"]}:sub(1,1)=="@" then\n'
        f'    local {ns["handle"]}={ns["io"]}.open({ns["src"]}:sub(2),"rb")\n'
        f'    if {ns["handle"]} then\n'
        f'      local {ns["body"]}={ns["handle"]}:read("*a")\n'
        f'      {ns["handle"]}:close()\n'
        f'      if type({ns["body"]})=="string" then\n'
        f'        local {ns["from"]}={ns["body"]}:find("{s1}".."{s2}",1,true)\n'
        f'        local {ns["to"]}={ns["body"]}:find("{e1}".."{e2}",1,true)\n'
        f'        if not {ns["from"]} or not {ns["to"]} or {ns["to"]}<={ns["from"]} then '
        f'{ns["fail"]}("integrity check failed") end\n'
        f'        local {ns["region"]}={ns["body"]}:sub({ns["from"]}+12,{ns["to"]}-3)\n'
        f'        {ns["region"]}={ns["region"]}:gsub("^[ \\t\\r\\n]+","")\n'
        f'        {ns["region"]}={ns["region"]}:gsub("[ \\t\\r\\n]+$","")\n'
        f'        local {ns["acc"]}=0\n'
        f'        for {ns["i"]}=1,#{ns["region"]} do\n'
        f'          local {ns["byte"]}={ns["region"]}:byte({ns["i"]})\n'
        f'          if {ns["byte"]}~=13 then '
        f'{ns["acc"]}=({ns["acc"]}*31+{ns["byte"]})%{CHECKSUM_MOD} end\n'
        f"        end\n"
        f'        if tostring({ns["acc"]})~={ns["sum"]} then '
        f'{ns["fail"]}("integrity check failed") end\n'
        f'        {ns["gate"]}={gate_value}\n'
        f"      end\n"
        f"    end\n"
        f"  end\n"
        f"end\n"
    )
    return head, ns["gate"], gate_value, (start_token, end_token)


def build_loader(
    inner_code: str,
    ngen: NameGenerator,
    rng: random.Random,
    label: str = "protected chunk",
    anti_debug: bool = True,
    chunk_name: str = "protected",
    layers: int = 1,
    self_check: bool = False,
) -> str:
    """Return a bootstrap that decrypts and runs ``inner_code``.

    ``layers`` nests further bootstraps: layer 1 decrypts to layer 2, which
    decrypts to ... and only the innermost layer is the program.
    ``self_check`` wraps all of it in a file-integrity gate.
    """
    if not self_check:
        code = inner_code
        for _ in range(max(1, int(layers))):
            code = _layer(code, ngen, rng, label, anti_debug, chunk_name)
        return code

    head, gate_name, gate_value, (start_token, end_token) = _self_check_head(ngen, rng, label)
    depth = max(1, int(layers))
    code = inner_code
    for level in range(depth):
        # only the outermost chunk can see the gate local; the nested chunks are
        # separate functions with their own upvalue-free scope
        is_outermost = level == depth - 1
        code = _layer(
            code,
            ngen,
            rng,
            label,
            anti_debug,
            chunk_name,
            gate=(gate_name, gate_value) if is_outermost else None,
        )

    # the hashed region is everything between the markers; the checksum literal
    # sits before it, so it never has to hash itself
    region = code
    if start_token in region or end_token in region:  # never ship a false positive
        raise RuntimeError("self-check marker collided with the payload")
    total = checksum(region.strip().encode("utf-8"))
    head = head.replace("__SELF_SUM__", str(total))
    return head + "--" + start_token + "\n" + region + "\n--" + end_token + "\n"
