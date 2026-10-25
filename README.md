# Lua Obfuscator

Turn readable Lua into code that is painful to read, while keeping it **exactly** as functional.

It is not a toy string-replacer: the tool contains a complete Lua 5.1–5.4 lexer, parser and
scope resolver, so every rewrite is performed on a real syntax tree. Variables are renamed
with correct scoping, string literals are encrypted with a runtime decoder, numbers become
arithmetic, conditions get opaque predicates, and dead code is sprinkled in — all while
`--verify` can run both versions and prove the behaviour is unchanged.

```
$ lua-obfuscator script.lua -o protected.lua --level heavy --verify
lua-obfuscator: behaviour verified: identical output
```

---

## Before / after

Input:

```lua
local secret = "admin"
local function check(password)
  if password == secret then
    print("access granted")
  else
    print("denied")
  end
end
check("admin")
check("guest")
```

Output (`--level medium --seed 7`, decoder prelude trimmed):

```lua
local _0x03b=-746703+10984906
local _0x2bb=_0x046"EgOfNYCG8bu+tw=="
local function _0x2ba(_0x2bc) if not((_0x03b*(_0x03b+(713452+-713451))+(814984+-814982))%(488219+-488217)==379147+-379146)and _0x2bc==_0x2bb then print(_0x046"BwWzyD8VlGqtJCjpCKiAU0uVNFIR")
if(_0x03b*(_0x03b+(314835+-314834))+(917649+-917647))%(764879+-764877)==-301925+301926 then local _0x03c=123801+-123426+(172976+-172379)
...
end else print(_0x046"AgTyRnqzrgl8krUC") end end
_0x2ba(_0x046"AgO8tFBjcEubtg==")
```

No identifier survives, no string is readable, and the program still prints
`access granted` / `denied`.

---

## Try it in the browser

```bash
python tools/playground.py        # -> http://localhost:8000
```

A zero-dependency playground: paste Lua on the left, pick a level, get obfuscated code on the
right, with a **verify** checkbox that runs both versions server-side before showing the result.
Useful for demoing or for quick one-off jobs.

---

## Install

```bash
git clone https://github.com/thescriptfinder628-cmyk/Lua-obfuscater-.git
cd Lua-obfuscater-
pip install -e .          # gives you the `lua-obfuscator` command
```

No dependencies. Python 3.9+.

You can also run it straight from the checkout without installing:

```bash
python -m luaobf script.lua -o out.lua
```

For the test suite and `--verify` you need a Lua interpreter — either
`pip install lupa` (embedded) or a `lua`/`lua5.4`/`luajit` binary on `PATH`.

---

## Usage

```bash
lua-obfuscator input.lua                          # to stdout, medium level
lua-obfuscator input.lua -o output.lua            # write a file
cat input.lua | lua-obfuscator > output.lua       # stdin works too
lua-obfuscator input.lua --level insane --seed 42 # reproducible chaos
lua-obfuscator input.lua --verify                 # run both, compare output
lua-obfuscator input.lua --stats --map names.json # report + renaming map
```

### Levels

| Level | What it does | Typical size |
|-------|--------------|--------------|
| `light` | rename locals + minify (comments, whitespace, blank lines gone) | **smaller** than input |
| `medium` | + encrypted strings & keys, encoded numbers, opaque predicates, some dead code | bigger |
| `heavy` | + confusable names, chunk wrapper, more dead code, decoy functions | much bigger |
| `insane` | + maximum dead code and decoys, hash names | largest |

Encrypted strings cost roughly 4/3 of their size plus a one-time ~1 KB decoder, so
anything from `medium` up will be larger than the input. `light` is a pure minifier.

### Options

| Flag | Meaning |
|------|---------|
| `-l, --level {light,medium,heavy,insane}` | preset (default `medium`) |
| `-o, --output FILE` | write to `FILE` instead of stdout |
| `--seed N` | make the output reproducible |
| `--names {short,wide,hash,confusable}` | identifier style |
| `--no-rename` | keep local variable names |
| `--no-strings` / `--no-numbers` / `--no-keys` | disable individual literal passes |
| `--no-control-flow` | no opaque predicates, no dead code |
| `--dead-code N` | dead-code density (0 disables) |
| `--junk-functions N` | add N decoy functions |
| `--wrap` / `--no-wrap` | wrap the chunk in `(function(...) … end)(...)` |
| `--pretty` | indent the output instead of minifying |
| `--verify` | execute original + obfuscated, compare behaviour |
| `--timeout SECONDS` | limit for each `--verify` run (default 10) |
| `--stats` | JSON statistics on stderr |
| `--map FILE` | write the `old → new` variable map as JSON |
| `--strict` | fail instead of warning (e.g. on dynamic globals) |
| `-q, --quiet` | silence warnings |

Exit codes: `0` success, `1` I/O error, `2` Lua syntax error, `3` internal error,
`4` `--verify` mismatch (nothing is written).

### Python API

```python
from luaobf import obfuscate

source = open("script.lua").read()
result = obfuscate(source, level="heavy", seed=1337, wrap=True)

print(result.code)          # obfuscated source
print(result.stats)         # sizes, seed, renaming map
print(result.warnings)      # e.g. "script uses dynamic name access (loadstring)"
```

Lower level control:

```python
from luaobf import obfuscate_source, preset

opts = preset("medium")
opts.rename_style = "confusable"
opts.encrypt_strings = False
opts.dead_code = 3
print(obfuscate_source(source, opts).code)
```

---

## How it works

The pipeline is `parse → resolve scopes → rename → control flow → encode literals → unparse`.

### 1. A real parser

`luaobf/lexer.py` and `luaobf/parser.py` implement the whole language: long strings and
comments at every bracket level, all escape sequences (`\z`, `\u{...}`, `\xNN`, `\ddd`),
hex floats, integer/float distinction, 5.3 bitwise operators, 5.4 `<const>`/`<close>`
attributes, labels and `goto`, method definitions, and varargs. Operator precedence follows
the reference manual exactly, including right-associative `..` and `^`.

### 2. Correct scope resolution

Renaming is only safe if every identifier is bound to the right declaration. `luaobf/resolve.py`
models the rules people usually get wrong:

* `local x = x` — the initialiser sees the **outer** `x`.
* `local function f` — `f` is visible inside its own body.
* `repeat … until cond` — locals declared in the body are visible in `cond`.
* `for i = a, b` — `a` and `b` are evaluated before `i` exists.
* `function t:m()` — there is an implicit `self` parameter, which is never renamed.
* Closures capture upvalues by reference; the same binding is renamed everywhere.

Generated names are **globally unique within the file** and never collide with a global the
script actually uses, so no new shadowing can be introduced.

### 3. Literal encryption

Every string becomes `dec("<payload>")`. The payload is
`[multiplier index][key length][key bytes][cipher bytes]`, then Base64. Each byte is
`(byte * multiplier + key[i]) % 256` where the multiplier is odd (so invertible mod 256),
with a fresh random multiplier and key **per string** — the same literal encodes differently
every time. The decoder is emitted once, caches its results, and uses pure arithmetic, so the
output still runs on Lua 5.1.

Numbers become `(a + b)`. Integers use exact integer arithmetic; floats are only rewritten
when Python can verify that `a + b` reproduces the original double bit-for-bit. Anything
risky (huge integers, `inf`, `nan`) is left alone.

Field names can be encrypted too: `t.name` becomes `t[dec("…")]`.

### 4. Control flow

Conditions get opaque predicates built from a junk constant `J`:

```lua
if (J*(J+1)+1) % 2 == 1 and cond then        -- always true
if (J*(J+1)+2) % 2 == 1 then <dead code> end -- always false
```

`J*(J+1)` is the product of two consecutive integers, hence always even — a fact that is
true of every integer, so no constant folder can reduce it without knowing `J`, and `J`
itself is only computed at runtime. `J` stays below 2^24 so the arithmetic is exact even on
Lua 5.1's doubles. Dead branches contain plausible-looking junk that never executes.

### 5. Clean output

`luaobf/unparser.py` re-prints the tree with correct parenthesisation (it knows that
`- -x` must not become `--x`, that `1 .. 2` must not become `1..2`, and that string
concatenation of keywords needs a space) and minifies whitespace and comments away.

---

## Safety

The obfuscator is conservative by design:

* Locals, parameters and loop variables are renamed; **globals never are**, because scripts
  reach them dynamically (`_G["x"]`, `getfenv`) and renaming would break them silently.
* If a script uses `loadstring`/`load`/`getfenv`/`debug`/`_ENV`/`setmetatable`, a warning is
  printed (and `--strict` turns it into an error). Renaming is still safe there — we just
  tell you the analysis is now best-effort.
* Generated code is re-parsed before it is returned; an internal bug cannot emit broken Lua.
* `--verify` executes the original and the obfuscated script in a fresh interpreter and
  compares stdout, exit status and the *kind* of any error. If they differ, nothing is written.

`--verify` runs your code twice, for real. Only use it on scripts you trust, and remember
that file/network side effects happen twice.

---

## What obfuscation cannot do

Be honest about the threat model:

* **It is not encryption.** Anyone can run the script; the strings get decrypted at runtime.
  A determined reverse-engineer can dump the decoded strings by patching `print`/`string.char`
  or by tracing the decoder.
* **Comments and structure are gone, meaning is not.** Control flow, table shapes and API
  calls are still inferable by an experienced reader or a deobfuscator.
* **Lua bytecode is not protected.** If you also ship `luac` output, that is a separate problem.
* Renaming changes names that appear in **error messages** and in `debug.getinfo`, which is
  usually desirable, but do not ship the output if something parses that text.
* Scripts that generate code at runtime (`load` on a built string) are not analysed; their
  contents are untouched.
* Obfuscation slightly slows startup (the decoder runs per unique string, once).

Used sensibly it reliably stops casual copying, "just open it in an editor and read the
logic" and naive string-grepping for keys or endpoints.

---

## Project layout

```
luaobf/
  lexer.py       Lua 5.1–5.4 lexer
  parser.py      recursive-descent parser -> AST
  ast_nodes.py   AST definitions
  resolve.py     scope resolution (bindings for every identifier)
  renamer.py     unique-name generation + write-back
  encoder.py     string encryption / number encoding + Lua decoder runtime
  control.py     opaque predicates, dead code, decoy functions
  unparser.py    AST -> Lua (pretty or minified)
  obfuscator.py  pipeline, options, presets
  verify.py      differential execution check
  cli.py         command line interface
tests/           unit tests + a Lua corpus run through real Lua
playground/      browser playground (single page, no build step)
tools/           web playground server + optional real-world stress test
```

## Testing

```bash
pip install lupa pyflakes     # test extras
python -m unittest discover -s tests -t .
```

The suite (88 tests) covers the lexer, parser, unparser, scope resolution, name generation,
the encryption round-trip, the CLI, and — most importantly — **differential tests**: every
script in `tests/scripts/` is obfuscated at all four levels with several seeds, executed in a
real Lua interpreter, and its output compared to the original.

Playground API tests run with the rest of the suite (the server itself is not started).

Optional real-world check (downloads MIT-licensed Lua libraries and verifies them):

```bash
python tools/check_realworld.py
#   kikito/inspect.lua   behaviour identical (all levels)
#   rxi/json.lua         behaviour identical (all levels)
#   kikito/middleclass   behaviour identical (all levels)
```

## License

MIT — see [LICENSE](LICENSE).
