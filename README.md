# Forsakenis4 — Lua Obfuscator

**Official obfuscation suite for every Lua dialect.** Turn readable Lua into code that is
extremely hostile to reverse-engineering, while keeping it *exactly* as functional.

The tool is not a string replacer: it contains a complete Lua 5.1–5.4 **and** Roblox Luau
lexer, parser, scope resolver and unparser. Every rewrite happens on a real syntax tree, and
every build can be **executed and diffed against the original** before you ship it.

```
$ lua-obfuscator script.lua --level nightmare --verify
lua-obfuscator: behaviour verified: identical output
```

---

## The site

```bash
python tools/playground.py      # -> http://localhost:8000
```

A self-contained blue web app (no build step, no CDN, no dependencies): paste Lua, choose a
level and dialect, hit **Obfuscate**. The *verify by running* switch executes both versions
server-side and refuses to return a build whose behaviour changed.

To put it on the internet, see **[PUBLISHING.md](PUBLISHING.md)** — `render.yaml`, `Dockerfile`,
`fly.toml` and `railway.json` are ready, so it is a three-click deploy.

---

## Before / after

Input:

```lua
local secret = "admin"
local function check(password)
  if password == secret then print("access granted") else print("denied") end
end
check("admin")
```

Output (`--level nightmare`, trimmed):

```lua
local OZ0=rawget(_G,"debug")
if type(OZ0)=="table" and type(OZ0.gethook)=="function" then
  local OZS=OZ0.gethook() if OZS~=nil then error("integrity check failed",0) end
end
local Zll=…; do local Z0O=7 while true do if Z0O==13 then … print(ZOZ"AQNi…") …
```

No identifier survives, no string is readable, and the statement order in the source no longer
matches execution order.

---

## Install

```bash
git clone https://github.com/thescriptfinder628-cmyk/Lua-obfuscater-.git
cd Lua-obfuscater-
pip install -e .        # provides `lua-obfuscator` and `forsakenis4`
```

Python 3.9+, no runtime dependencies. For `--verify` and the test suite you also need a Lua
interpreter: `pip install lupa` (embedded) or a `lua`/`lua5.4`/`luajit` binary on `PATH`.

---

## Usage

```bash
lua-obfuscator input.lua -o protected.lua                  # medium, auto target
lua-obfuscator input.lua --level nightmare --verify        # maximum strength, proven
lua-obfuscator script.luau --target lua51 -o script.lua    # Luau source -> plain Lua 5.1
lua-obfuscator input.lua --flatten 2 --strings vm --seed 42
lua-obfuscator input.lua --anti-debug --brand Forsakenis4
```

### Levels

| Level | Adds | Size |
|-------|------|------|
| `light` | renamed locals, comments/whitespace removed | **smaller** than input |
| `medium` | encrypted strings & keys, encoded numbers, opaque predicates | ~3–5× |
| `heavy` | control-flow flattening, chunk wrapper, decoy functions | ~5–8× |
| `insane` | per-string bytecode VM instead of one cipher | ~7–10× |
| `nightmare` | everything + an **encrypted self-loading bootstrap**, anti-debug guards | ~10–36× |

### Dialects (`--target`)

| Target | Notes |
|--------|-------|
| `auto` *(default)* | reads the script and picks the narrowest target that preserves its exact behaviour |
| `lua51` | stock 5.1: `goto`, bitwise ops and `//` are lowered or refused with a clear error |
| `lua52` | adds `goto`; `continue` lowers to real labels; bitwise emulated arithmetically |
| `lua53` / `lua54` | native bitwise, `//` and integers; 5.4 keeps `<const>`/`<close>` |
| `luajit` | LuaJIT 2.x (5.1 dialect + `goto`) |
| `luau` | Roblox: types, `continue`, if-expressions, compound assignment kept natively |

**Luau in, Lua out** works: types are erased, `continue` and if-expressions are lowered,
`x += 1` is expanded with the receiver evaluated exactly once. **Lua in, Luau out** works too.

### Key options

| Flag | Meaning |
|------|---------|
| `-l, --level` | preset (`light` … `nightmare`) |
| `-t, --target` | output dialect (above) |
| `--strings {cipher,vm}` | string protection strategy |
| `--flatten 0-3` | control-flow flattening intensity |
| `--anti-debug` | emit runtime integrity guards |
| `--loader` / `--no-loader` | hide the whole program in an encrypted bootstrap that `load`s itself (`nightmare` does this by default) |
| `--brand NAME` | embed a hidden watermark (only recoverable by decoding) |
| `--seed N` | reproducible builds |
| `--names {short,wide,hash,confusable}` | identifier style |
| `--no-rename` / `--no-strings` / `--no-numbers` / `--no-keys` / `--no-control-flow` | disable passes |
| `--wrap` / `--no-wrap` | wrap the chunk in `(function(...) … end)(...)` |
| `--pretty` | indent instead of minify |
| `--verify` | run original + build, compare behaviour (refuses to write on mismatch) |
| `--stats`, `--map FILE`, `--strict`, `-q` | reporting and strictness |

Exit codes: `0` ok, `1` I/O, `2` Lua syntax error, `3` internal, `4` verify mismatch, `5` unsupported for target.

### Python API

```python
from luaobf import obfuscate

result = obfuscate(open("script.lua").read(), level="nightmare", target="lua51", seed=1337)
print(result.code, result.stats, result.warnings)
```

---

## How it works

```
parse (auto dialect) → resolve scopes → flatten → opaque predicates/dead code
   → encrypt literals → lower to target → rename → unparse
```

**1. Real parsing.** Long strings/comments at every bracket level, all escapes (`\z`,
`\u{…}`, `\xNN`, `\ddd`), hex floats, integer/float distinction, 5.3 bitwise operators, 5.4
attributes, labels/`goto`, method sugar, varargs — plus Luau types, generics, casts,
compound assignment, `continue` and if-expressions.

**2. Correct scoping.** `local x = x` reads the outer `x`; `repeat … until` exposes body
locals to its condition; `for` bounds are evaluated before the loop variable; `function t:m()`
has an implicit `self`; closures capture by reference. Generated names are unique per file and
never collide with a used global, so no new shadowing can appear.

**3. Control-flow flattening.** A body is cut into segments and re-emitted as a state machine
whose branches are printed in random order with random state numbers. Segment cuts are only
allowed where no local is used later, so scoping is unchanged; `repeat` bodies, blocks with
escaping jumps and `goto` blocks are left alone (correctness over noise).

**4. String protection.** *cipher* mode: `(byte * multiplier + key[i]) % 256` with a fresh
multiplier and key per string, Base64 payload, one cached decoder. *vm* mode: every string
carries its own 3–7 op program (add / multiply / rotate) interpreted by a generated bytecode
loop — there is no single algorithm to reverse. Both are pure arithmetic, so they run on 5.1.

**5. Numbers.** Integers become `a+b`; floats are rewritten only when Python proves the exact
IEEE-754 round trip; anything risky is left untouched.

**6. Lowering.** `continue`, `goto`, if-expressions, bitwise operators, `//` and attributes are
translated for whatever target you asked for — or the pass raises instead of emitting code that
would silently misbehave.

**7. Verification.** `--verify` executes both programs in a fresh interpreter and compares
stdout, exit status and the *kind* of any error.

---

## Attack it yourself

The project ships the attacker, because a protection claim you cannot measure is marketing:

```bash
python tools/attack_demo.py protected.lua --original script.lua
```

It runs your build with the instrumentation a real analyst uses — `string.char` hook,
`table.concat` hook, a registry/memory walk, and plain output observation — and reports how
much it got back. Measured on a script with 6 string literals (including an API key):

| Build | `string.char` + `table.concat` | memory walk | output watch |
|-------|-------------------------------|-------------|--------------|
| `medium` (single cipher) | **6/6 literals recovered** | 6/6 | 6/6 |
| `nightmare` (per-string VM) | **0/6 recovered** | 0/6* | **6/6** |

`*` the walk cannot see function locals — a real process-memory dump can, so treat that cell as
"still vulnerable", not as safe.

So the honest summary of what changed: the **cheap, scriptable dumpers are closed** — the thing
that lets a beginner steal your keys in ten lines is gone. What remains is observation: your
program must eventually hand data to `print`, an HTTP client or the GPU in plaintext, and that
moment can be watched. No obfuscator on earth changes that.

---

## The loader: nothing readable left in the file

`--loader` (on by default in `nightmare`) is the last mile of static protection. The entire
obfuscated program is compressed, encrypted, Base64-encoded and prepended with a checksum,
then decoded **at run time** by a small bootstrap:

```lua
local <fail>=function(m) error("protected chunk: "..tostring(m),0) end
-- [anti-debug guard, optionally]
-- [string-VM decoder]
local <blob>="…"                        -- the whole program, as ciphertext
local <sum>=0
for <i>=1,#<blob> do <sum>=(<sum>*31+(<blob>):byte(<i>))%2147483647 end
if <sum>~=<expected> then <fail>("checksum mismatch") end
local <load>=loadstring or load
local <chunk>=<load>(<decoded>,"@protected")
return <chunk>(...)
```

What that changes for an attacker reading the file:

| | before | with the loader |
|---|---|---|
| `function` keywords in the file | dozens (real logic) | ~7 (the decoder only) |
| `if` statements | all of them | 2 (guard + checksum) |
| any original identifier | 0 (renamed) | 0 |
| any original string literal | 0 | 0 |
| program structure | visible, just ugly | **absent** |
| edit one byte of the blob | n/a | `checksum mismatch`, refuses to run |

Measured with `tools/attack_demo.py` on a 335-byte script:

```
static surface (readable without running it): 7 functions, 6 ifs, 22 calls, 67 statements
EXACT literal recovered : 0/6
```

The honest part: this stops *reading* the file, not *running* it. The chunk has to exist in
memory to execute, so a dumper that hooks `load` gets the plaintext of the inner chunk back —
exactly as any packed binary can be dumped. Anti-debug (`--anti-debug`) raises that cost, and
the string VM means the recovered chunk still has no readable literals in it. That is the
ceiling for a self-contained file, and this is where it sits.

## Honest limits

You asked for something that cannot be deobfuscated. That does not exist for *any* tool, and
here is exactly why:

* **The Lua VM must execute the code.** Whatever it decrypts or computes at runtime is present
  in memory, so a memory dump or an emulator defeats every source-level trick.
* **Strings have to become plaintext** before `print`/`string.char` sees them — a traced
  `string.char` recovers them, one string at a time in VM mode.
* **Flattening is a puzzle, not a secret.** A determined analyst can rebuild the control flow
  from the state numbers; it costs hours, not centuries.
* **Bytecode is separate.** `luac` output needs its own protection.

What this tool *does* achieve is the practical ceiling: no readable names, strings, structure
or comments; execution order hidden behind randomized states; a string layer with no fixed
algorithm, no fixed opcode dispatch, and no `string.char`/`table.concat` for a dumper to hook
(measured above: 0/6 recovered by the cheap dumpers); and a verified-identical build. That stops
casual copying, "open it in an editor", every grep-based attack, and the standard Lua string
dumpers — and makes serious analysis expensive.

Also be aware: renaming changes identifiers that appear in **error messages** and
`debug.getinfo`; `--anti-debug` will fire if a tracing hook is installed (some anti-cheats
install one); and `--verify` runs your script twice, so file/network side effects happen twice.

---

## Project layout

```
luaobf/
  lexer.py parser.py ast_nodes.py     Lua + Luau front end
  resolve.py renamer.py               scoping and unique renaming
  flatten.py                          control-flow flattening
  encoder.py stringvm.py              string/number protection
  control.py                          opaque predicates, dead code, decoys
  lower.py targets.py                 multi-dialect output
  unparser.py                         AST -> Lua (pretty or minified)
  obfuscator.py cli.py verify.py      pipeline, CLI, differential checker
playground/index.html                 the blue Forsakenis4 web app
tools/playground.py                   zero-dependency server for it
tools/attack_demo.py                  measures how much an attacker recovers
tools/check_realworld.py              optional real-library stress test
tests/                                124 tests + a Lua corpus
```

## Testing

```bash
pip install lupa pyflakes
python -m unittest discover -s tests -t .
```

The important tests are differential: every script in `tests/scripts/` is obfuscated at all
five levels, across dialects and seeds, executed in a real Lua interpreter, and compared
against the original's output. Development was driven by them — they caught a decoder name
mismatch, a mathematically wrong opaque predicate (`J*J+1` is not always odd), a shuffled
execution order in the flattener, `repeat`/`until` scope breakage, and `break` escaping the
state machine.

The attack tool is part of the suite too, so the security claims can never silently rot:
`tests/test_attack_demo.py` asserts that a cipher build *does* leak to cheap hooks and a
hardened build *does not*.

Optional real-world check (MIT-licensed libraries from GitHub):

```bash
python tools/check_realworld.py
```

## License

MIT — see [LICENSE](LICENSE). © Forsakenis4
