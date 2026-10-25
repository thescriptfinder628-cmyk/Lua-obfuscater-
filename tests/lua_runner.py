"""Run Lua code (original vs. obfuscated) and compare observable behaviour.

Uses ``lupa`` (a real Lua interpreter) so tests compare actual execution rather
than text.  If ``lupa`` is unavailable the helpers raise ``SkipTest``.
"""

from __future__ import annotations

import re

try:  # pragma: no cover - environment dependent
    import lupa
    from lupa import LuaRuntime

    HAVE_LUA = True
    LUA_VERSION = None
except Exception:  # pragma: no cover
    HAVE_LUA = False
    LUA_VERSION = None


PRELUDE = r"""
__out = {}
function __p(...)
  local n = select('#', ...)
  local parts = {}
  for i = 1, n do
    local v = select(i, ...)
    parts[#parts + 1] = tostring(v)
  end
  __out[#__out + 1] = table.concat(parts, "\t")
end
print = __p
"""


class ExecResult:
    def __init__(self, output: list[str], error: str | None, globals_snapshot: dict | None = None):
        self.output = output
        self.error = error
        self.globals = globals_snapshot or {}

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ExecResult) and self.output == other.output and self.error == other.error

    def __repr__(self) -> str:
        return f"ExecResult(output={self.output!r}, error={self.error!r})"


def run_lua(source: str, globals_after: tuple[str, ...] = ()) -> ExecResult:
    """Execute ``source`` in a fresh Lua state and capture ``print`` output."""
    if not HAVE_LUA:  # pragma: no cover
        raise RuntimeError("lupa is not installed")
    runtime = LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(PRELUDE)
    error = None
    try:
        runtime.execute(source)
    except Exception as exc:  # noqa: BLE001 - Lua errors surface as Python exceptions
        error = f"{type(exc).__name__}: {exc}".replace("\n", " ")
        # normalise the file/line prefix so that line numbers don't matter
        error = _normalise_error(error)
    out = runtime.globals().__out
    raw = [out[i] for i in range(1, len(out) + 1)] if out else []
    output = [_normalise_output(line) for line in raw]
    snapshot = {}
    g = runtime.globals()
    for name in globals_after:
        try:
            snapshot[name] = repr(g[name])
        except Exception:
            snapshot[name] = "<missing>"
    return ExecResult(output, error, snapshot)


_ADDR_RE = re.compile(r"(function|table|userdata|thread): 0x[0-9a-fA-F]+")


def _normalise_output(line: str) -> str:
    """Remove memory addresses so two runs can be compared."""
    return _ADDR_RE.sub(r"\1: 0xADDR", line)


def _normalise_error(text: str) -> str:
    """Drop file/line/address noise so original and obfuscated errors compare equal."""
    text = text.split("stack traceback:")[0]
    text = _ADDR_RE.sub(r"\1: 0xADDR", text)
    text = re.sub(r"\[string \"[^\"]*\"\]", "", text)
    text = re.sub(r":\d+:", ":", text)
    return text.strip()


def assert_same_behaviour(original: str, obfuscated: str, label: str = "") -> ExecResult:
    """Raise ``AssertionError`` unless both programs behave identically."""
    before = run_lua(original)
    after = run_lua(obfuscated)
    if before.output != after.output:
        raise AssertionError(
            f"{label}: output mismatch\n  original: {before.output!r}\n  obfuscated: {after.output!r}"
            f"\n  original error: {before.error}\n  obfuscated error: {after.error}"
        )
    if (before.error is None) != (after.error is None):
        raise AssertionError(
            f"{label}: error mismatch\n  original: {before.error}\n  obfuscated: {after.error}"
            f"\n  output: {after.output!r}"
        )
    return after


def available() -> bool:
    return HAVE_LUA
