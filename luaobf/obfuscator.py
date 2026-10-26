"""The obfuscation pipeline and its presets."""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

from . import ast_nodes as A
from . import stringvm
from .control import apply_control_flow, junk_declaration
from .encoder import apply_encoder
from .flatten import apply_flatten
from .loader import build_loader
from .lower import LoweringError, lower_chunk
from .parser import parse
from .renamer import NameGenerator, rename
from .resolve import DYNAMIC_GLOBALS, resolve
from .targets import Target, detect_dialect, get_target
from .unparser import unparse


@dataclass
class Options:
    #: preset this option set came from (informational)
    level: str = "medium"

    #: output runtime: auto | lua51 | lua52 | lua53 | lua54 | luajit | luau
    target: str = "auto"

    # naming
    rename: bool = True
    rename_style: str = "hash"  # short | wide | hash | confusable

    # literals
    encrypt_strings: bool = True
    encrypt_numbers: bool = True
    encrypt_keys: bool = True
    #: "cipher" (fast, one cached decoder) or "vm" (per-string bytecode program)
    string_mode: str = "cipher"

    # control flow
    control_flow: bool = True
    dead_code: int = 1
    junk_functions: int = 0
    wrap_conditions: bool = True
    #: 0 = off, 1 = normal, 2 = aggressive control-flow flattening
    flatten: int = 0

    # hardening
    anti_debug: bool = False
    #: hide the entire program behind an encrypted self-loading bootstrap
    loader: bool = False
    #: name shown in error messages from the loader
    chunk_name: str = "protected"
    #: number of nested encrypted bootstraps (1 = single layer)
    layers: int = 1
    #: verify a checksum of the file itself before decoding anything
    self_check: bool = False
    #: hidden watermark, recoverable only by decoding
    brand: str = ""

    # structure / output
    wrap: bool = False
    minify: bool = True

    # misc
    seed: int | None = None
    strict: bool = False
    strip_comments: bool = True

    def resolved_seed(self) -> int:
        if self.seed is None:
            return random.randrange(1, 2**31)
        return self.seed


PRESETS: dict[str, Options] = {
    "light": Options(
        level="light",
        rename_style="short",
        encrypt_strings=False,
        encrypt_numbers=False,
        encrypt_keys=False,
        control_flow=False,
    ),
    "medium": Options(level="medium"),
    "heavy": Options(
        level="heavy",
        rename_style="confusable",
        dead_code=2,
        junk_functions=2,
        wrap=True,
        flatten=1,
    ),
    "insane": Options(
        level="insane",
        rename_style="hash",
        dead_code=3,
        junk_functions=4,
        wrap=True,
        flatten=2,
        string_mode="vm",
    ),
    "nightmare": Options(
        level="nightmare",
        rename_style="confusable",
        dead_code=4,
        junk_functions=8,
        wrap=True,
        flatten=2,
        string_mode="vm",
        anti_debug=True,
        loader=True,
        layers=3,
    ),
}


def preset(name: str, **overrides) -> Options:
    if name not in PRESETS:
        raise ValueError(f"unknown level {name!r}; choose from {', '.join(PRESETS)}")
    opts = replace(PRESETS[name])
    for key, value in overrides.items():
        if value is not None:
            setattr(opts, key, value)
    return opts


@dataclass
class Result:
    code: str
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


SHEBANG_PREFIX = "#!"

ANTI_DEBUG_SOURCE = """local $dg=rawget(_G,"debug")
if type($dg)=="table" and type($dg.gethook)=="function" then
  local $h=$dg.gethook()
  if $h~=nil then error("integrity check failed",0) end
end
"""


def _anti_debug_source(ngen: NameGenerator) -> str:
    text = ANTI_DEBUG_SOURCE
    for token in ("dg", "h"):
        text = text.replace("$" + token, ngen.next())
    return text


def _uses_goto(chunk: A.Chunk) -> bool:
    from .renamer import walk

    return any(isinstance(node, (A.Goto, A.Label)) for node in walk(chunk))


def _uses_bitwise(chunk: A.Chunk) -> bool:
    from .renamer import walk

    for node in walk(chunk):
        if isinstance(node, A.BinOp) and node.op in ("&", "|", "~", "<<", ">>"):
            return True
        if isinstance(node, A.UnOp) and node.op == "~":
            return True
    return False


def _uses_floor_div(chunk: A.Chunk) -> bool:
    from .renamer import walk

    return any(isinstance(node, A.BinOp) and node.op == "//" for node in walk(chunk))


def obfuscate_source(source: str, options: Options | None = None, filename: str = "<input>") -> Result:
    opts = options or Options()
    seed = opts.resolved_seed()
    rng = random.Random(seed)
    warnings: list[str] = []

    shebang = ""
    body_source = source
    if body_source.startswith(SHEBANG_PREFIX):
        newline = body_source.find("\n")
        if newline == -1:
            shebang, body_source = body_source + "\n", ""
        else:
            shebang, body_source = body_source[: newline + 1], body_source[newline + 1 :]

    # ---- target / dialect -------------------------------------------------
    target: Target = get_target(opts.target)
    dialect = "luau" if target.dialect == "luau" else "auto"
    if opts.target == "auto":
        detected = detect_dialect(body_source)
        if detected == "luau":
            warnings.append("Luau syntax detected; pass --target luau to keep `continue`/types out of the output")
    chunk = parse(body_source, filename, dialect=dialect)

    # A script that uses newer syntax cannot run on stock Lua 5.1.  If the target
    # was not chosen explicitly, quietly pick the nearest target that preserves
    # the original semantics exactly (emulation can change numeric subtypes).
    if target.name == "lua51":
        needs = []
        if _uses_goto(chunk):
            needs.append(("lua52", "goto/labels"))
        if _uses_bitwise(chunk) or _uses_floor_div(chunk):
            needs.append(("lua53", "bitwise operators or //"))
        if needs:
            upgraded, why = needs[-1]
            if opts.target == "auto":
                target = get_target(upgraded)
                warnings.append(f"script uses {why}; emitting {upgraded} instead of lua51")
            else:
                raise LoweringError(
                    f"the script uses {why}, which {target.name} does not support; "
                    f"use --target {upgraded} or newer"
                )

    if not chunk.body.stats:
        return Result(
            code=shebang,
            stats={
                "seed": seed,
                "input_bytes": len(source.encode("utf-8")),
                "output_bytes": len(shebang.encode("utf-8")),
                "renamed": 0,
                "names": {},
                "level": opts.level,
                "target": target.name,
            },
            warnings=[],
        )

    resolver = resolve(chunk)
    if opts.rename:
        dynamic = sorted(resolver.dynamic_features & DYNAMIC_GLOBALS)
        if dynamic:
            warnings.append(
                "script uses dynamic name access (" + ", ".join(dynamic) + "); "
                "renaming locals is still safe, but globals are left untouched"
            )
    ngen = NameGenerator(opts.rename_style, reserved=set(resolver.globals_used), seed=rng.randrange(1000))

    # ---- control-flow flattening -----------------------------------------
    flattened = 0
    if opts.flatten:
        flattened = apply_flatten(chunk, rng, ngen, resolver, intensity=opts.flatten)

    # ---- opaque predicates / dead code ------------------------------------
    if opts.control_flow:
        junk_var = apply_control_flow(
            chunk,
            rng,
            ngen,
            wrap_conditions=opts.wrap_conditions,
            dead_code=opts.dead_code,
            junk_functions=opts.junk_functions,
        )
        chunk.body.stats.insert(0, junk_declaration(junk_var, rng))

    # ---- literals ---------------------------------------------------------
    prelude_parts: list[str] = []
    if opts.anti_debug:
        prelude_parts.append(_anti_debug_source(ngen))

    names_map: dict[str, str] = {}
    if opts.encrypt_strings or opts.encrypt_numbers:
        if opts.string_mode == "vm" and opts.encrypt_strings:
            prelude, encoder = apply_encoder(
                chunk,
                ngen,
                rng,
                encrypt_numbers=opts.encrypt_numbers,
                encrypt_keys=opts.encrypt_keys,
                string_mode="vm",
            )
        else:
            prelude, encoder = apply_encoder(
                chunk,
                ngen,
                rng,
                encrypt_numbers=opts.encrypt_numbers,
                encrypt_keys=opts.encrypt_keys and opts.encrypt_strings,
            )
        if prelude:
            prelude_parts.append(prelude)

    # ---- hidden watermark -------------------------------------------------
    if opts.brand and opts.encrypt_strings:
        brand_name = ngen.next()
        payload_name = ngen.next()
        encoded = stringvm.encode(opts.brand.encode("utf-8"), rng) if opts.string_mode == "vm" else None
        if encoded is None:
            from .encoder import encode_payload

            encoded = encode_payload(opts.brand.encode("utf-8"), rng)
        decoder = encoder.decoder_name if (opts.encrypt_strings or opts.encrypt_numbers) else brand_name
        prelude_parts.append(f'local {brand_name}={decoder}"{encoded}"\n')
        prelude_parts.append(f"local {payload_name}={brand_name}\n")

    # ---- lower to the target dialect --------------------------------------
    helper_prelude, lower_warnings = lower_chunk(chunk, target)
    if helper_prelude:
        prelude_parts.append(helper_prelude)
    warnings.extend(lower_warnings)

    # ---- rename (last, so generated helpers are renamed too) --------------
    if opts.rename:
        resolver = resolve(chunk)
        names_map = rename(chunk, resolver, style=opts.rename_style, seed=rng.randrange(1000))

    # ---- wrap -------------------------------------------------------------
    if opts.wrap:
        wrapper = A.Function([], True, chunk.body)
        chunk = A.Chunk(A.Block([A.Return([A.Call(wrapper, [A.Vararg()])])]))

    code = unparse(chunk, minify=opts.minify)
    if prelude_parts:
        code = "".join(prelude_parts) + ("\n" if opts.minify else "\n\n") + code
    if shebang:
        code = shebang + code

    if opts.loader:
        # Hide the whole program behind an encrypted bootstrap: after this the
        # file contains no Lua logic at all, only a decoder and a blob.
        code = build_loader(
            code,
            ngen,
            rng,
            # a generic prefix keeps the --brand watermark out of the file
            label="protected chunk",
            anti_debug=opts.anti_debug,
            chunk_name=opts.chunk_name,
            layers=opts.layers,
            self_check=opts.self_check,
        )

    # ---- sanity: the output must parse for the target ---------------------
    try:
        parse(code[len(shebang) :], "<obfuscated>", dialect=target.dialect)
    except Exception as exc:  # pragma: no cover - a bug in a pass
        raise RuntimeError(f"internal error: generated code does not parse: {exc}") from exc

    stats = {
        "seed": seed,
        "input_bytes": len(source.encode("utf-8")),
        "output_bytes": len(code.encode("utf-8")),
        "renamed": len(names_map),
        "names": names_map,
        "level": opts.level,
        "target": target.name,
        "flattened": flattened,
        "string_mode": opts.string_mode if opts.encrypt_strings else "off",
        "loader": bool(opts.loader),
        "layers": int(opts.layers) if opts.loader else 0,
        "self_check": bool(opts.self_check and opts.loader),
    }
    if opts.strict and warnings:
        raise RuntimeError("; ".join(warnings))
    return Result(code=code, stats=stats, warnings=warnings)


def obfuscate_file(path: str, options: Options | None = None, encoding: str = "utf-8") -> Result:
    with open(path, "rb") as handle:
        raw = handle.read()
    source = raw.decode(encoding)
    return obfuscate_source(source, options, filename=path)
