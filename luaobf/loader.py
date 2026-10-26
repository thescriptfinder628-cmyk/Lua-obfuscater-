"""Wrap the whole program in an encrypted, self-loading bootstrap.

Static analysis is the cheapest way to attack an obfuscated file: open it, read
it.  The loader removes that option entirely.  The protected file contains only:

* a decoder (the same per-string VM used for literals),
* the *encrypted* program as a Base64 blob,
* a checksum, and a call to ``load``.

There is no Lua logic in the file to read -- not even renamed logic.  To get the
program back, an analyst must execute the file while hooking ``load``/
``loadstring``, and what they then recover is the *inner* build, which is itself
flattened with VM-encrypted literals.

Layout of the emitted file (names are random per build)::

    local <fail> = function(m) error("<label>: " .. m, 0) end
    local <dbg> = rawget(_G, "debug")
    if <dbg> and <dbg>.gethook() ~= nil then <fail>("integrity") end
    <decoder runtime>
    local <blob> = <decode>("<base64 payload>")
    local <sum> = 0
    for i = 1, #<blob> do <sum> = (<sum> * 31 + <blob>:byte(i)) % 2147483647 end
    if <sum> ~= <checksum> then <fail>("checksum") end
    local <loader> = loadstring or load
    local <chunk> = <loader>(<blob>, "@<name>")
    if type(<chunk>) ~= "function" then <fail>("load") end
    return <chunk>(...)

What this defeats: reading the file, grepping it, static decompilers, and any
tool that does not actually run the program.  What it does not defeat: running
the program and dumping memory -- nothing can, because the program must execute.
"""

from __future__ import annotations

import random

from . import stringvm
from .renamer import NameGenerator

CHECKSUM_MOD = 2147483647


def checksum(data: bytes) -> int:
    """``s = (s*31 + byte) % 2147483647`` -- identical in Python and Lua."""
    total = 0
    for byte in data:
        total = (total * 31 + byte) % CHECKSUM_MOD
    return total


def build_loader(
    inner_code: str,
    ngen: NameGenerator,
    rng: random.Random,
    label: str = "protected chunk",
    anti_debug: bool = True,
    chunk_name: str = "protected",
) -> str:
    """Return a bootstrap that decrypts and runs ``inner_code``."""
    raw = inner_code.encode("utf-8")
    payload = stringvm.encode(raw, rng)
    # the bootstrap checksums what it gets back from the decoder, i.e. the
    # plaintext (``raw``), not the Base64 wrapper
    total = checksum(raw)

    decoder_names = {token: ngen.next() for token in stringvm.NAME_TOKENS}
    fail = ngen.next()
    dbg = ngen.next()
    blob = ngen.next()
    acc = ngen.next()
    index = ngen.next()
    loader = ngen.next()
    chunk = ngen.next()

    guard = ""
    if anti_debug:
        guard = (
            f'local {dbg}=rawget(_G,"debug")\n'
            f'if {dbg} and type({dbg}.gethook)=="function" and {dbg}.gethook()~=nil then '
            f'{fail}("integrity check failed") end\n'
        )

    return (
        f'local {fail}=function(m) error("{label}: "..tostring(m),0) end\n'
        + guard
        + stringvm.runtime_source(decoder_names)
        + f'local {blob}={decoder_names["dec"]}"{payload}"\n'
        f"local {acc}=0\n"
        f"for {index}=1,#{blob} do {acc}=({acc}*31+({blob}):byte({index}))%{CHECKSUM_MOD} end\n"
        f"if {acc}~={total} then {fail}(\"checksum mismatch\") end\n"
        f"local {loader}=loadstring or load\n"
        f"local {chunk}={loader}({blob},\"@{chunk_name}\")\n"
        f'if type({chunk})~="function" then {fail}("bootstrap failed") end\n'
        f"return {chunk}(...)\n"
    )
