"""Dialect (target runtime) definitions.

The obfuscator can emit code for every mainstream Lua implementation.  Features
that a target lacks are lowered automatically by :mod:`luaobf.lower`, so a Luau
script can be delivered as plain Lua 5.1 (for an old game client), and a Lua 5.4
script can be delivered as Luau (for Roblox).

Targets
-------
================  =========================================================
``lua51``         PUC Lua 5.1 / 5.0 era. No ``goto``, no bitwise operators,
                  no ``//``, no integer subtype.
``lua52``         Adds ``goto``; still no bitwise operators.
``lua53``         Adds bitwise operators, ``//`` and integers.
``lua54``         Adds ``<const>``/``<close>`` attributes.
``luajit``        LuaJIT 2.x (Lua 5.1 dialect + ``goto``).
``luau``          Roblox Luau: types, ``continue``, if-expressions, compound
                  assignment, but **no** ``goto`` and no bitwise operators.
``auto``          Detect from the input, defaulting to ``lua51``-compatible
                  output (the widest audience).
================  =========================================================
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    name: str
    version: str
    #: language capabilities
    has_goto: bool = False
    has_bitwise: bool = False
    has_integer_division: bool = False
    has_attributes: bool = False
    has_continue: bool = False
    has_if_expr: bool = False
    has_types: bool = False
    #: dialect used by the parser for this target
    dialect: str = "lua"
    description: str = ""

    @property
    def needs_goto_lowering(self) -> bool:
        return not self.has_goto

    @property
    def needs_bitwise_lowering(self) -> bool:
        return not self.has_bitwise

    @property
    def needs_fdiv_lowering(self) -> bool:
        return not self.has_integer_division

    @property
    def needs_continue_lowering(self) -> bool:
        return not self.has_continue

    @property
    def needs_ifexpr_lowering(self) -> bool:
        return not self.has_if_expr


TARGETS: dict[str, Target] = {
    "lua51": Target(
        "lua51", "5.1", description="PUC Lua 5.1 (widest compatibility)"
    ),
    "lua52": Target(
        "lua52", "5.2", has_goto=True, description="PUC Lua 5.2"
    ),
    "lua53": Target(
        "lua53",
        "5.3",
        has_goto=True,
        has_bitwise=True,
        has_integer_division=True,
        description="PUC Lua 5.3",
    ),
    "lua54": Target(
        "lua54",
        "5.4",
        has_goto=True,
        has_bitwise=True,
        has_integer_division=True,
        has_attributes=True,
        description="PUC Lua 5.4",
    ),
    "luajit": Target(
        "luajit",
        "LuaJIT 2.x",
        has_goto=True,
        description="LuaJIT 2.x (5.1 dialect with goto)",
    ),
    "luau": Target(
        "luau",
        "Luau",
        has_continue=True,
        has_if_expr=True,
        has_types=True,
        dialect="luau",
        description="Roblox Luau",
    ),
}

DEFAULT_TARGET = "lua51"


def get_target(name: str) -> Target:
    if name == "auto":
        return TARGETS[DEFAULT_TARGET]
    if name not in TARGETS:
        raise ValueError(
            f"unknown target {name!r}; choose from {', '.join(sorted(TARGETS))}, or auto"
        )
    return TARGETS[name]


def detect_dialect(source: str) -> str:
    """Cheap heuristic used only for reporting, never for correctness."""
    import re

    luau_markers = [
        r"^\s*(export\s+)?type\s+\w+\s*(<|=)",          # type declarations
        r"\blocal\s+\w+\s*:\s*[A-Za-z_{]",              # local x: number
        r"\bfunction\s+[\w.:]+\s*\([^)]*\w\s*:",        # typed parameters
        r"\)\s*:\s*[A-Za-z_{]",                          # return types
        r"\bfunction\s*<",                                # generic functions
        r"::\s*[A-Za-z_{]",                                # casts
        r"^\s*continue\s*$",                              # continue statements
        r"\b(if|elseif)\b[^\n]*\bthen\b[^\n]*\belse\b[^\n]*$",  # if-expressions
    ]
    for pattern in luau_markers:
        if re.search(pattern, source, re.MULTILINE):
            return "luau"
    if "--!strict" in source or "--!nonstrict" in source:
        return "luau"
    return "lua"


def supports(target: Target, feature: str) -> bool:
    return bool(getattr(target, f"has_{feature}", False))
