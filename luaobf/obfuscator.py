"""The obfuscation pipeline and its presets."""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

from . import ast_nodes as A
from .control import apply_control_flow, junk_declaration
from .encoder import apply_encoder
from .parser import parse
from .renamer import NameGenerator, rename
from .resolve import DYNAMIC_GLOBALS, resolve
from .unparser import unparse


@dataclass
class Options:
    #: preset name this option set came from (informational)
    level: str = "medium"

    # naming
    rename: bool = True
    rename_style: str = "hash"  # short | wide | hash | confusable

    # literals
    encrypt_strings: bool = True
    encrypt_numbers: bool = True
    encrypt_keys: bool = True

    # control flow
    control_flow: bool = True
    dead_code: int = 1
    junk_functions: int = 0
    wrap_conditions: bool = True

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
    ),
    "insane": Options(
        level="insane",
        rename_style="hash",
        dead_code=3,
        junk_functions=4,
        wrap=True,
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

    chunk = parse(body_source, filename)

    if not chunk.body.stats:
        # nothing to hide; an empty chunk (or a lone shebang) stays empty
        return Result(
            code=shebang,
            stats={
                "seed": seed,
                "input_bytes": len(source.encode("utf-8")),
                "output_bytes": len(shebang.encode("utf-8")),
                "renamed": 0,
                "names": {},
                "level": opts.level,
            },
            warnings=[],
        )

    resolver = resolve(chunk)

    if opts.rename:
        dynamic = sorted(resolver.dynamic_features & DYNAMIC_GLOBALS)
        if dynamic:
            msg = (
                "script uses dynamic name access ("
                + ", ".join(dynamic)
                + "); renaming locals is still safe, but globals are left untouched"
            )
            warnings.append(msg)
    ngen = NameGenerator(opts.rename_style, reserved=set(resolver.globals_used), seed=rng.randrange(1000))

    mapping: dict[str, str] = {}
    if opts.rename:
        mapping = rename(chunk, resolver, style=opts.rename_style, seed=rng.randrange(1000))

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

    prelude = ""
    if opts.encrypt_strings or opts.encrypt_numbers:
        prelude, _encoder = apply_encoder(
            chunk,
            ngen,
            rng,
            encrypt_numbers=opts.encrypt_numbers,
            encrypt_keys=opts.encrypt_keys and opts.encrypt_strings,
        )

    if opts.wrap:
        body = chunk.body
        varargs = A.Vararg()
        wrapper = A.Function([], True, body)
        chunk = A.Chunk(A.Block([A.Return([A.Call(wrapper, [varargs])])]))

    code = unparse(chunk, minify=opts.minify)
    if prelude:
        code = prelude + ("\n" if opts.minify else "\n") + code
    if shebang:
        code = shebang + code

    # sanity: the output must still parse
    try:
        parse(code if not shebang else code[len(shebang):], "<obfuscated>")
    except Exception as exc:  # pragma: no cover - a bug in a pass
        raise RuntimeError(f"internal error: generated code does not parse: {exc}") from exc

    stats = {
        "seed": seed,
        "input_bytes": len(source.encode("utf-8")),
        "output_bytes": len(code.encode("utf-8")),
        "renamed": len(mapping),
        "names": mapping,
        "level": opts.level,
    }
    if opts.strict and warnings:
        raise RuntimeError("; ".join(warnings))
    return Result(code=code, stats=stats, warnings=warnings)


def obfuscate_file(path: str, options: Options | None = None, encoding: str = "utf-8") -> Result:
    with open(path, "rb") as handle:
        raw = handle.read()
    source = raw.decode(encoding)
    return obfuscate_source(source, options, filename=path)
