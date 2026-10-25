"""Rename local variables, parameters and loop variables.

Only *locals* are renamed.  Globals are left alone by default because a script
may reach them dynamically (``_G["x"]``, ``getfenv()``, ``load()``…), and
renaming those would break it.  Because every generated name is unique across
the whole chunk and never collides with a global that the script touches,
shadowing cannot introduce new behaviour.
"""

from __future__ import annotations

import string

from . import ast_nodes as A
from .lexer import KEYWORDS
from .resolve import Binding, Resolver

# Character sets used to build new identifiers.  ``confusable`` deliberately
# mixes glyphs that are hard to tell apart in most fonts (l/I/1, O/0, S/5 …).
CHARSETS = {
    "short": string.ascii_lowercase,
    "wide": string.ascii_letters + "_",
    "hash": "0123456789abcdef",
    "confusable": "lI1O0S5Z2",
}

PREFIXES = {
    "short": "",
    "wide": "",
    "hash": "_0x",
    "confusable": "",
}


class NameGenerator:
    """Produces unique, never-used identifiers in a chosen visual style."""

    def __init__(self, style: str = "hash", reserved: set[str] | None = None, seed: int = 0) -> None:
        if style not in CHARSETS:
            raise ValueError(f"unknown name style {style!r}")
        self.style = style
        self.charset = CHARSETS[style]
        self.prefix = PREFIXES[style]
        self.reserved = set(reserved or ())
        self.reserved |= set(KEYWORDS)
        self.issued: set[str] = set()
        self.counter = seed

    def _candidate(self, index: int) -> str:
        base = len(self.charset)
        digits = []
        n = index
        while True:
            digits.append(self.charset[n % base])
            n = n // base - 1
            if n < 0:
                break
        return self.prefix + "".join(reversed(digits))

    def next(self) -> str:
        while True:
            name = self._candidate(self.counter)
            self.counter += 1
            if not name or name[0].isdigit():
                continue
            if name in self.reserved or name in self.issued:
                continue
            self.issued.add(name)
            return name


def reserved_names(resolver: Resolver) -> set[str]:
    """Names we must never generate: used globals + protected identifiers."""
    reserved = set(resolver.globals_used)
    reserved |= resolver.protected
    # any identifier that survives renaming (fields, labels, method names)
    # doesn't matter for locals, but keep globals out of the way.
    return reserved


def rename(chunk: A.Chunk, resolver: Resolver, style: str = "hash", seed: int = 0) -> dict[str, str]:
    """Assign new names to every renamable binding and write them back.

    Returns a mapping ``{old_name: new_name}`` of the first N renames (useful
    for a human-readable report; names repeat across scopes, so this is only
    informational).
    """
    reserved = reserved_names(resolver)
    # Also avoid the original names of locals that we are *not* renaming, so
    # that a generated name can never shadow them.
    for binding in resolver.bindings:
        if not binding.renamable:
            reserved.add(binding.name)
    rng = NameGenerator(style, reserved=reserved, seed=seed)

    # Most-referenced variables get the shortest names (cheap minification win).
    order = sorted(
        (b for b in resolver.bindings if b.renamable),
        key=lambda b: (-b.refs, b.kind, b.name),
    )
    for binding in order:
        binding.new_name = rng.next()

    _writeback(chunk, resolver)
    mapping: dict[str, str] = {}
    for binding in order:
        mapping.setdefault(binding.name, binding.new_name)
    return mapping


def _writeback(chunk: A.Chunk, resolver: Resolver) -> None:
    for binding in resolver.bindings:
        node = binding.node
        if isinstance(node, A.LocalDecl):
            node.new_names[binding.slot] = binding.new_name
        elif isinstance(node, A.Param):
            node.new_name = binding.new_name
        elif isinstance(node, A.LocalFunction):
            node.new_name = binding.new_name
        elif isinstance(node, A.NumFor):
            node.new_name = binding.new_name
        elif isinstance(node, A.GenFor):
            node.new_names[binding.slot] = binding.new_name
        else:  # pragma: no cover - defensive
            raise TypeError(f"cannot write back binding for {type(node).__name__}")

    # every Name reference, plus the root of `function a.b.c()` paths
    for node in walk(chunk):
        if isinstance(node, A.Name):
            if isinstance(node.binding, Binding) and node.binding.new_name:
                node.new_name = node.binding.new_name
        elif isinstance(node, A.FuncStat):
            root = getattr(node, "path_ref", None)
            if root is not None and isinstance(root.binding, Binding) and root.binding.new_name:
                node.path[0] = root.binding.new_name


def walk(root: A.Node):
    """Yield every AST node below (and including) ``root``."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        for field in getattr(node, "_fields", ()):
            value = getattr(node, field, None)
            if isinstance(value, A.Node):
                stack.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, A.Node):
                        stack.append(item)
                    elif isinstance(item, tuple):
                        for sub in item:
                            if isinstance(sub, A.Node):
                                stack.append(sub)
