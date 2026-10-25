"""Command line interface for the Lua obfuscator."""

from __future__ import annotations

import argparse
import json
import sys

from .lexer import LuaSyntaxError
from .obfuscator import PRESETS, Options, obfuscate_source, preset

VERSION = "1.0.0"

EPILOG = """\
examples:
  lua-obfuscator script.lua -o out.lua
  lua-obfuscator script.lua --level heavy --seed 1337
  cat script.lua | lua-obfuscator --level light > out.lua
  lua-obfuscator script.lua --level medium --stats --map names.json
  lua-obfuscator script.lua --verify        # run both versions, compare output

levels:
  light    rename locals + minify (smallest output, fastest)
  medium   + encrypted strings/numbers + opaque predicates  (default)
  heavy    + method hiding + chunk wrapper + dead code
  insane   maximum noise: more dead code, junk functions, hash names
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lua-obfuscator",
        description="Make Lua source code unreadable while keeping it functional.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", default="-", help="input .lua file ('-' for stdin)")
    parser.add_argument("-o", "--output", help="output file (default: stdout)")
    parser.add_argument("-l", "--level", choices=sorted(PRESETS), default="medium", help="preset strength")
    parser.add_argument("--seed", type=int, help="random seed for reproducible output")
    parser.add_argument("--names", choices=("short", "wide", "hash", "confusable"), help="identifier style")

    parser.add_argument("--no-rename", action="store_true", help="keep local variable names")
    parser.add_argument("--no-strings", action="store_true", help="do not encrypt string literals")
    parser.add_argument("--no-numbers", action="store_true", help="do not encode number literals")
    parser.add_argument("--no-keys", action="store_true", help="do not encrypt field-name keys (t.x)",)
    parser.add_argument("--no-control-flow", action="store_true", help="skip opaque predicates and dead code")
    parser.add_argument("--dead-code", type=int, help="dead-code density, 0 disables (0-5)")
    parser.add_argument("--junk-functions", type=int, help="number of unused decoy functions")
    parser.add_argument("--wrap", action="store_true", help="wrap the chunk in an anonymous function")
    parser.add_argument("--no-wrap", action="store_true", help="never wrap the chunk")
    parser.add_argument("--pretty", action="store_true", help="indent the output instead of minifying")

    parser.add_argument("--strict", action="store_true", help="fail on warnings (e.g. dynamic globals)")
    parser.add_argument("--stats", action="store_true", help="print statistics to stderr as JSON")
    parser.add_argument("--map", metavar="FILE", help="write the local-variable renaming map to FILE")
    parser.add_argument("--check", action="store_true", help="(always on) verify the output parses")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="run the original and obfuscated script and compare their output (needs lua or lupa)",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds allowed per --verify run")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress warnings")
    parser.add_argument("--version", action="version", version=f"lua-obfuscator {VERSION}")
    return parser


def options_from_args(args: argparse.Namespace) -> Options:
    opts = preset(args.level)
    if args.names:
        opts.rename_style = args.names
    if args.no_rename:
        opts.rename = False
    if args.no_strings:
        opts.encrypt_strings = False
    if args.no_numbers:
        opts.encrypt_numbers = False
    if args.no_keys:
        opts.encrypt_keys = False
    if args.no_control_flow:
        opts.control_flow = False
    if args.dead_code is not None:
        opts.dead_code = max(0, args.dead_code)
        opts.control_flow = opts.control_flow and opts.dead_code > 0
    if args.junk_functions is not None:
        opts.junk_functions = max(0, args.junk_functions)
        opts.control_flow = True if opts.junk_functions else opts.control_flow
    if args.wrap:
        opts.wrap = True
    if args.no_wrap:
        opts.wrap = False
    if args.pretty:
        opts.minify = False
    if args.seed is not None:
        opts.seed = args.seed
    if args.strict:
        opts.strict = True
    return opts


def read_input(path: str) -> str:
    if path == "-":
        data = sys.stdin.buffer.read()
    else:
        with open(path, "rb") as handle:
            data = handle.read()
    # tolerate a UTF-8 BOM
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    opts = options_from_args(args)

    try:
        source = read_input(args.input)
    except OSError as exc:
        print(f"lua-obfuscator: cannot read {args.input}: {exc.strerror}", file=sys.stderr)
        return 1

    try:
        result = obfuscate_source(source, opts, filename=args.input)
    except LuaSyntaxError as exc:
        print(f"lua-obfuscator: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"lua-obfuscator: {exc}", file=sys.stderr)
        return 3

    if result.warnings and not args.quiet:
        for warning in result.warnings:
            print(f"lua-obfuscator: warning: {warning}", file=sys.stderr)

    if args.verify:
        from .verify import VerificationUnavailable, verify

        try:
            ok, message = verify(source, result.code, timeout=args.timeout)
        except VerificationUnavailable as exc:
            print(f"lua-obfuscator: warning: cannot verify: {exc}", file=sys.stderr)
        else:
            if not ok:
                print(f"lua-obfuscator: {message}", file=sys.stderr)
                print("lua-obfuscator: refusing to write output (use without --verify to force)", file=sys.stderr)
                return 4
            if not args.quiet:
                print(f"lua-obfuscator: {message}", file=sys.stderr)

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(result.code)
        except OSError as exc:
            print(f"lua-obfuscator: cannot write {args.output}: {exc.strerror}", file=sys.stderr)
            return 1
    else:
        sys.stdout.write(result.code)
        if not result.code.endswith("\n"):
            sys.stdout.write("\n")

    if args.map:
        with open(args.map, "w", encoding="utf-8") as handle:
            json.dump(result.stats["names"], handle, indent=2, sort_keys=True)
            handle.write("\n")

    if args.stats:
        stats = dict(result.stats)
        stats.pop("names", None)
        ratio = stats["output_bytes"] / stats["input_bytes"] if stats["input_bytes"] else 0
        stats["ratio"] = round(ratio, 2)
        print(json.dumps(stats, indent=2), file=sys.stderr)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
