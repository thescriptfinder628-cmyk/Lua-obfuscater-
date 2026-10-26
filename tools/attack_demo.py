#!/usr/bin/env python3
"""Attacker-in-a-box: recover what an analyst recovers from a protected build.

This tool exists to keep the project honest. It takes a protected file, runs it
in a real Lua interpreter with the standard instrumentation an analyst would use,
and reports what falls out:

    1. ``concat``   -- hook ``table.concat`` (the classic string-dumper trick)
    2. ``char``     -- hook ``string.char`` and log assembled characters
    3. ``memory``   -- after execution, scan reachable tables/strings (a stand-in
                       for a memory dump: this is the attack that cannot be
                       prevented, only made noisier)

Optionally pass ``--original`` to see how many of the source's string literals
were recovered, which is the number that actually matters.

Usage
-----
    python tools/attack_demo.py protected.lua
    python tools/attack_demo.py protected.lua --original script.lua

Nothing here is malicious: it only executes the build you give it, in the same
sandbox as ``--verify``.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from luaobf.parser import parse  # noqa: E402
from luaobf.renamer import walk  # noqa: E402
import luaobf.ast_nodes as A  # noqa: E402

HARNESS = r"""
__found = {}
__seen = {}

local function __record(text)
  if type(text) ~= "string" or #text < 3 then return end
  local printable = 0
  for i = 1, #text do
    local b = text:byte(i)
    if b >= 32 and b < 127 then printable = printable + 1 end
  end
  if printable / #text >= 0.85 and not __seen[text] then
    __seen[text] = true
    __found[#__found + 1] = text
  end
end

local __concat = table.concat
local __char = string.char
local __sub = string.sub

local __print = print

function __install(mode)
  if mode == "output" then
    -- the attack that always works: watch what the program DOES
    print = function(...)
      for i = 1, select("#", ...) do __record(tostring((select(i, ...)))) end
      return __print(...)
    end
  elseif mode == "concat" then
    table.concat = function(...)
      local result = __concat(...)
      __record(result)
      return result
    end
  elseif mode == "char" then
    string.char = function(...)
      local b = __char(...)
      __buffer = (__buffer or "") .. b
      if #__buffer > 400 then __record(__buffer) __buffer = "" end
      return b
    end
  end
end

function __restore()
  table.concat = __concat
  string.char = __char
  print = __print
end

function __scan()
  -- stand-in for a memory dump: walk everything reachable from the registry
  local seen = {}
  local function walk(value, depth)
    if depth > 6 then return end
    local kind = type(value)
    if kind == "string" then __record(value) return end
    if kind ~= "table" and kind ~= "function" then return end
    if seen[value] then return end
    seen[value] = true
    if kind == "table" then
      for k, v in pairs(value) do
        walk(k, depth + 1)
        walk(v, depth + 1)
      end
    end
  end
  walk(debug.getregistry(), 0)
end

function __attack(code, limit, use_hook)
  __found = {}
  __seen = {}
  if use_hook then
    local n = 0
    debug.sethook(function() n = n + 1 if n > limit then error("TOOLONG", 2) end end, "", 500)
  end
  local chunk, loaderr = load(code, "@build")
  local ok, err
  if not chunk then ok, err = false, loaderr else ok, err = pcall(chunk) end
  if use_hook then debug.sethook() end
  __scan()
  return __found, ok, err
end
"""


def lua_string_literals(source: str) -> list[str]:
    """All string literals in a Lua source file (what an attacker wants)."""
    try:
        chunk = parse(source)
    except Exception:
        return []
    out = []
    for node in walk(chunk):
        if isinstance(node, A.String):
            try:
                text = node.value.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if len(text) >= 3 and text.isprintable():
                out.append(text)
    return out


STRATEGIES = {
    # name -> (hook to install, install a debug hook?)
    "concat": ("concat", True),
    "char": ("char", True),
    "memory": (None, True),
    "unhooked": (None, False),
    "output": ("output", False),
}


def static_surface(source: str) -> dict:
    """How much Lua logic can a reader see without executing anything?"""
    try:
        chunk = parse(source)
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc)}
    counts = {"statements": 0, "functions": 0, "ifs": 0, "calls": 0, "strings": 0}
    for node in walk(chunk):
        if isinstance(node, (A.Stat,)):
            counts["statements"] += 1
        if isinstance(node, (A.Function, A.LocalFunction, A.FuncStat)):
            counts["functions"] += 1
        if isinstance(node, A.If):
            counts["ifs"] += 1
        if isinstance(node, (A.Call, A.MethodCall)):
            counts["calls"] += 1
        if isinstance(node, A.String):
            counts["strings"] += 1
    return counts


def attack(build_source: str, mode: str, limit: int = 400000):
    from lupa import LuaRuntime

    hook, use_hook = STRATEGIES[mode]
    runtime = LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(HARNESS)
    if hook:
        runtime.execute("__install('" + hook + "')")
    try:
        found, ok, err = runtime.eval("__attack")(build_source, limit, use_hook)
    finally:
        runtime.execute("__restore()")
    results = [found[i] for i in range(1, len(found) + 1)] if found else []
    return results, ok, (str(err) if err else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover strings from a protected Lua build")
    parser.add_argument("build", help="the protected .lua file")
    parser.add_argument("--original", help="the original script, to measure recovery")
    parser.add_argument("--limit", type=int, default=400000, help="instruction budget")
    args = parser.parse_args(argv)

    with open(args.build, encoding="utf-8") as handle:
        build_source = handle.read()

    targets: list[str] = []
    if args.original:
        with open(args.original, encoding="utf-8") as handle:
            targets = lua_string_literals(handle.read())

    print(f"build: {args.build} ({len(build_source)} bytes)")
    surface = static_surface(build_source)
    if "error" not in surface:
        print(
            "static surface (readable without running it): "
            f"{surface['functions']} functions, {surface['ifs']} ifs, "
            f"{surface['calls']} calls, {surface['statements']} statements"
        )
    if targets:
        print(f"original string literals worth stealing: {len(targets)}")

    all_found: set[str] = set()
    for mode in ("concat", "char", "memory", "unhooked", "output"):
        found, ok, err = attack(build_source, mode, args.limit)
        unique = set(found)
        all_found |= unique
        status = "ran" if ok else f"stopped early ({err.splitlines()[0] if err else 'error'})"
        print(f"\n  strategy '{mode}': {len(unique)} distinct strings   [{status}]")
        for text in sorted(unique)[:8]:
            shown = text if len(text) <= 70 else text[:67] + "…"
            print(f"      {shown!r}")
        if len(unique) > 8:
            print(f"      … and {len(unique) - 8} more")

    print(f"\n  TOTAL recovered: {len(all_found)} distinct strings")
    if targets:
        exact = [t for t in targets if t in all_found]
        # A literal counts as leaked if it appears *inside* anything recovered:
        # seeing "?key=sk_live_…" in program output leaks the key just as well.
        embedded = [t for t in targets if any(t in found for found in all_found)]
        pct_exact = 100 * len(exact) / len(targets)
        pct_any = 100 * len(embedded) / len(targets)
        print(f"  EXACT literal recovered : {len(exact)}/{len(targets)} ({pct_exact:.0f}%)")
        print(f"  LEAKED (exact or inside): {len(embedded)}/{len(targets)} ({pct_any:.0f}%)")
        missing = [t for t in targets if t not in embedded]
        if missing:
            print("  still hidden: " + ", ".join(repr(m[:40]) for m in missing))
    print(
        "\n  how to read this:\n"
        "    concat / char  -> the cheap dumpers everyone uses. 0 recovered = closed.\n"
        "    memory         -> registry walk. Needs an instruction hook, so anti-debug\n"
        "                      can stop it -- but it also CANNOT see function locals,\n"
        "                      so a low number here does NOT mean the strings are safe.\n"
        "    unhooked       -> same walk without any hook (bypasses anti-debug).\n"
        "    output         -> just watches what the program prints. This always works.\n"
        "\n  the 'output' strategy is the ceiling. A program must hand its data to a\n"
        "  real receiver (print, an HTTP client, the GPU) in plaintext, so observing\n"
        "  behaviour recovers the secrets no matter how they were stored. A process\n"
        "  memory dump does the same thing without needing the program to cooperate.\n"
        "  Obfuscation raises the cost of reading the CODE; it cannot hide what the\n"
        "  code ultimately does."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
