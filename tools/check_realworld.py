#!/usr/bin/env python3
"""Optional stress test: download real Lua libraries and check obfuscation.

Requires network access to api.github.com and an interpreter (``lupa`` or a
``lua`` binary) for the behaviour comparison::

    python tools/check_realworld.py
    python tools/check_realworld.py --levels heavy insane

For each library the script (1) parses the original, (2) obfuscates it at every
requested level, (3) loads the result in a fresh Lua state, and (4) where a
driver is defined, calls exported functions and compares the output of the
original and obfuscated module.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luaobf.obfuscator import PRESETS, obfuscate_source, preset  # noqa: E402
from luaobf.parser import parse  # noqa: E402
from luaobf.unparser import quote_string  # noqa: E402
from luaobf.verify import VerificationUnavailable, run  # noqa: E402

#: (repo, path, name) triples.  Small, dependency-free, MIT-licensed libraries.
LIBRARIES = [
    ("kikito/inspect.lua", "inspect.lua", "inspect"),
    ("rxi/json.lua", "json.lua", "json"),
    ("kikito/middleclass", "middleclass.lua", "middleclass"),
]

DRIVERS = {
    "inspect": """
print(M({1,2,3}))
print(M({1,{2,{3}}}))
print(M("str"), M(nil), M(true), M(42))
""",
    "json": """
print(M.encode({1,2,3}))
print(M.encode({a=1,b={true,false},c="hi"}))
print(M.encode(M.decode('{"a":[1,{"b":2}],"c":{"d":[true,null]}}')))
""",
    "middleclass": """
local Animal = M("Animal")
function Animal:initialize(n) self.name = n end
function Animal:speak() return self.name .. "!" end
local Dog = M("Dog", Animal)
local d = Dog("Rex")
print(d:speak(), d.name, Dog.name)
""",
}


def fetch(repo: str, path: str) -> str:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github.raw"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def harness(module_source: str, driver: str) -> str:
    literal = quote_string(module_source.encode("utf-8"))
    return f"local __src = assert(load({literal}, '@mod'))\nlocal M = __src()\n{driver}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", nargs="+", default=list(PRESETS), choices=sorted(PRESETS))
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--keep", metavar="DIR", help="save the downloaded sources here")
    args = parser.parse_args(argv)

    workdir = args.keep or tempfile.mkdtemp(prefix="luaobf-realworld-")
    os.makedirs(workdir, exist_ok=True)

    failures = 0
    for repo, path, name in LIBRARIES:
        print(f"\n=== {repo}/{path}")
        try:
            source = fetch(repo, path)
        except Exception as exc:  # noqa: BLE001
            print(f"  download failed: {exc}")
            failures += 1
            continue
        with open(os.path.join(workdir, path.replace("/", "_")), "w", encoding="utf-8") as handle:
            handle.write(source)

        parse(source, path)
        print(f"  parsed original: {len(source)} bytes")

        driver = DRIVERS.get(name, "")
        baseline = None
        if driver:
            try:
                baseline = run(harness(source, driver))
            except VerificationUnavailable as exc:
                print(f"  cannot execute: {exc}")

        for level in args.levels:
            result = obfuscate_source(source, preset(level, seed=args.seed), filename=path)
            parse(result.code, "<obfuscated>")
            status = "parses"
            if baseline is not None:
                try:
                    actual = run(harness(result.code, driver))
                except Exception as exc:  # noqa: BLE001
                    status = f"ERROR {exc}"
                    failures += 1
                else:
                    if actual.matches(baseline):
                        status = "behaviour identical"
                    else:
                        status = "BEHAVIOUR CHANGED"
                        failures += 1
            print(f"  {level:7s} {len(result.code):7d} bytes  {status}")

    print(f"\n{'FAILURES: %d' % failures if failures else 'all real-world checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
