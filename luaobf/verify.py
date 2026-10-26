"""Run a script before and after obfuscation and compare what it does.

This is the strongest safety net the tool has: instead of trusting a list of
rules, actually execute both programs and check that the observable behaviour
matches (stdout, stderr and exit status).

Two backends are supported, in order of preference:

1. ``lupa``  -- an embedded Lua interpreter (no external binary needed);
2. a ``lua``/``lua5.4``/``lua5.3``/``luajit`` binary on ``PATH``.

Only use this on scripts you trust: they are executed twice, for real.  Scripts
that read input, write files or talk to the network will do so twice.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

LUA_BINARIES = ("lua", "lua5.4", "lua5.3", "lua5.2", "lua5.1", "luajit")


class VerificationUnavailable(RuntimeError):
    """No Lua interpreter could be found."""


class _Timeout(Exception):
    pass


def find_lua() -> str | None:
    for name in LUA_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _have_lupa() -> bool:
    try:
        import lupa  # noqa: F401
    except Exception:
        return False
    return True


def backend() -> str | None:
    if _have_lupa():
        return "lupa"
    if find_lua():
        return "lua"
    return None


class RunOutput:
    def __init__(self, stdout: str, stderr: str, status: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.status = status

    def matches(self, other: "RunOutput") -> bool:
        return (
            self.stdout == other.stdout
            and self.status == other.status
            and _first_line(self.stderr) == _first_line(other.stderr)
        )

    def describe(self) -> str:
        return f"status={self.status} stdout={self.stdout!r} stderr={self.stderr!r}"


def _first_line(text: str) -> str:
    """Error text without file:line prefixes (line numbers move when we rewrite)."""
    if not text.strip():
        return ""
    import re

    line = text.strip().splitlines()[0]
    line = re.sub(r"^\[string [^\]]*\]", "", line)
    # a run from a file reports "LuaError: /tmp/xyz.lua:39: …"; the path and the
    # line number both move between the original and the build
    line = re.sub(r"[^\s:]*\.lua:\d+:\s*", "", line)
    line = re.sub(r"^[^\s:]+\.lua:\d+:", "", line)
    line = re.sub(r":\d+:", ":", line)
    # identifiers are renamed on purpose, so an error mentioning `x` in the
    # original and `_0x1f` in the output must still count as the same error
    line = re.sub(r"""(['"])[^'"]*\1""", "'?'", line)
    return line.strip()


def _run_with_lupa(source: str, timeout: float) -> RunOutput:
    from lupa import LuaRuntime

    runtime = LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(
        """
        local __buf = {}
        local __out, __err = table.concat, table.concat
        function print(...)
          local parts = {}
          for i = 1, select('#', ...) do parts[i] = tostring((select(i, ...))) end
          __buf[#__buf + 1] = table.concat(parts, '\\t')
        end
        function __result() return table.concat(__buf, '\\n') end
        """
    )
    status = 0
    stderr = ""
    # run from a file, not a string: that is how the user will run it, and
    # --self-check builds refuse to start when they cannot read their own source
    handle, path = tempfile.mkstemp(suffix=".lua")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(source)
        try:
            runtime.eval("function(p) local f = assert(loadfile(p)) return f() end")(path)
        except Exception as exc:  # noqa: BLE001
            status = 1
            stderr = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = runtime.eval("__result()") or ""
    return RunOutput(out, stderr, status)


def _run_with_binary(binary: str, source: str, timeout: float) -> RunOutput:
    handle, path = tempfile.mkstemp(suffix=".lua")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(source)
        proc = subprocess.run(
            [binary, path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return RunOutput(proc.stdout, proc.stderr, proc.returncode)
    except subprocess.TimeoutExpired:
        return RunOutput("", "<timeout>", 124)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def run(source: str, timeout: float = 10.0, prefer: str | None = None) -> RunOutput:
    which = prefer or backend()
    if which is None:
        raise VerificationUnavailable(
            "no Lua interpreter found: install `lupa` (pip install lupa) "
            "or put a `lua` binary on PATH"
        )
    if which == "lupa":
        return _run_with_lupa(source, timeout)
    binary = find_lua()
    if not binary:  # pragma: no cover - backend() already checked
        raise VerificationUnavailable("no lua binary on PATH")
    return _run_with_binary(binary, source, timeout)


def verify(original: str, obfuscated: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Return ``(ok, message)`` after comparing the two programs."""
    if backend() is None:
        raise VerificationUnavailable(
            "no Lua interpreter found: install `lupa` (pip install lupa) "
            "or put a `lua` binary on PATH"
        )
    try:
        before = run(original, timeout)
        after = run(obfuscated, timeout)
    except VerificationUnavailable:
        raise
    if before.matches(after):
        return True, "behaviour verified: identical output"
    details = [
        "obfuscated program does not behave like the original",
        f"  original : {before.describe()}",
        f"  obfuscated: {after.describe()}",
    ]
    return False, "\n".join(details)
