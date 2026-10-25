"""Recursive-descent parser producing the luaobf AST.

Covers the full Lua 5.1 – 5.4 statement/expression grammar: numeric and generic
``for``, ``goto``/labels, method definitions and calls, varargs, table
constructors with all three field forms, 5.4 ``<const>``/``<close>`` attributes
and the 5.3 bitwise operators.

Dialect ``luau`` additionally accepts the Roblox/Luau extensions: type
annotations (``local x: number``, ``(a: string) -> boolean``, generics),
``type``/``export type`` declarations, ``::`` casts, compound assignment
(``+=``, ``..=`` …), ``continue`` and ``if … then … else …`` expressions.
These are lowered or dropped before output.
"""

from __future__ import annotations

from . import ast_nodes as A
from .lexer import EOF, KEYWORD, NAME, NUMBER, OP, STRING, LuaSyntaxError, Lexer, Token

BLOCK_END_KW = {"end", "else", "elseif", "until"}

#: (left binding power, right binding power).  Right-associative operators have
#: right < left.
BINARY_PRIORITY: dict[str, tuple[int, int]] = {
    "or": (1, 2),
    "and": (2, 3),
    "<": (3, 4),
    ">": (3, 4),
    "<=": (3, 4),
    ">=": (3, 4),
    "~=": (3, 4),
    "==": (3, 4),
    "|": (4, 5),
    "~": (5, 6),
    "&": (6, 7),
    "<<": (7, 8),
    ">>": (7, 8),
    "..": (9, 8),
    "+": (10, 11),
    "-": (10, 11),
    "*": (11, 12),
    "/": (11, 12),
    "//": (11, 12),
    "%": (11, 12),
    "^": (14, 13),
}

UNARY_PRIORITY = 12
UNARY_OPS = {"not", "-", "#", "~"}


#: Tokens that can continue a Luau type expression after a complete atom.
TYPE_CONTINUATIONS = {"|", "&", "?", "->", ".", "[", "<", "(", "{"}

#: Compound assignment operators and the plain operator they expand to.
COMPOUND_OPS = {
    "+=": "+",
    "-=": "-",
    "*=": "*",
    "/=": "/",
    "//=": "//",
    "%=": "%",
    "^=": "^",
    "..=": "..",
}


class Parser:
    def __init__(
        self,
        tokens: list[Token],
        filename: str | None = None,
        dialect: str = "lua",
    ) -> None:
        self.tokens = tokens
        self.pos = 0
        self.filename = filename
        self.dialect = dialect
        #: names invented while lowering Luau compound assignments
        self._tmp_counter = 0
        self._used_names = {tok.value for tok in tokens if tok.kind == NAME}

    @property
    def luau(self) -> bool:
        return self.dialect == "luau"

    def fresh_local(self, hint: str = "t") -> str:
        while True:
            self._tmp_counter += 1
            name = f"__{hint}{self._tmp_counter}"
            if name not in self._used_names:
                self._used_names.add(name)
                return name

    # -- token helpers -----------------------------------------------------

    @property
    def tok(self) -> Token:
        return self.tokens[self.pos]

    def lookahead(self, off: int = 1) -> Token:
        j = min(self.pos + off, len(self.tokens) - 1)
        return self.tokens[j]

    def next(self) -> Token:
        t = self.tokens[self.pos]
        if t.kind != EOF:
            self.pos += 1
        return t

    def error(self, message: str, tok: Token | None = None):
        t = tok or self.tok
        near = "EOF" if t.kind == EOF else repr(t.raw)
        raise LuaSyntaxError(f"{message} near {near}", t.line, t.col, self.filename)

    def check_op(self, *ops: str) -> bool:
        return self.tok.kind == OP and self.tok.value in ops

    def check_kw(self, *kws: str) -> bool:
        return self.tok.kind == KEYWORD and self.tok.value in kws

    def accept_op(self, op: str) -> bool:
        if self.check_op(op):
            self.next()
            return True
        return False

    def accept_kw(self, kw: str) -> bool:
        if self.check_kw(kw):
            self.next()
            return True
        return False

    def expect_op(self, op: str) -> Token:
        if not self.check_op(op):
            self.error(f"'{op}' expected")
        return self.next()

    def expect_kw(self, kw: str) -> Token:
        if not self.check_kw(kw):
            self.error(f"'{kw}' expected")
        return self.next()

    def expect_name(self) -> Token:
        if self.tok.kind != NAME:
            self.error("<name> expected")
        return self.next()

    def _mark(self, node, tok: Token):
        node.line = tok.line
        node.col = tok.col
        return node

    # -- entry point -------------------------------------------------------

    def parse_chunk(self) -> A.Chunk:
        body = self.parse_block()
        if self.tok.kind != EOF:
            self.error("'<eof>' expected")
        return A.Chunk(body)

    # -- blocks ------------------------------------------------------------

    def parse_block(self) -> A.Block:
        stats: list[A.Stat] = []
        while True:
            t = self.tok
            if t.kind == EOF:
                break
            if t.kind == KEYWORD and t.value in BLOCK_END_KW:
                break
            if t.kind == KEYWORD and t.value == "return":
                stats.append(self.parse_return())
                break
            if t.kind == KEYWORD and t.value == "break":
                self.next()
                stats.append(self._mark(A.Break(), t))
                # Lua 5.1 requires break to be the last statement in a block;
                # keep parsing (5.2+ allows more code) but remember position.
                continue
            stat = self.parse_statement()
            if stat is not None:
                stats.append(stat)
        return A.Block(stats)

    def parse_return(self) -> A.Return:
        t = self.expect_kw("return")
        exprs: list[A.Expr] = []
        if not (
            self.tok.kind == EOF
            or (self.tok.kind == KEYWORD and self.tok.value in BLOCK_END_KW)
            or self.check_op(";")
        ):
            exprs = self.parse_exprlist()
        self.accept_op(";")
        return self._mark(A.Return(exprs), t)

    # -- statements --------------------------------------------------------

    def parse_statement(self) -> A.Stat | None:
        t = self.tok

        if self.check_op(";"):
            self.next()
            return None

        if self.check_op("::"):
            self.next()
            name = self.expect_name().value
            self.expect_op("::")
            return self._mark(A.Label(name), t)

        # Luau adds contextual keywords that are plain identifiers in Lua:
        # `type X = ...`, `export type X = ...` and `continue`.
        if self.luau and t.kind == NAME:
            if t.value == "type" and self.lookahead().kind == NAME:
                return self.parse_type_decl(False)
            if t.value == "export" and self.lookahead().kind == NAME and self.lookahead().value == "type":
                self.next()
                return self.parse_type_decl(True)
            if t.value == "continue" and not self.check_op("=", ",", ".", "[", "(", ":", "{"):
                self.next()
                return self._mark(A.Continue(), t)

        if t.kind == KEYWORD:
            kw = t.value
            if kw == "if":
                return self.parse_if()
            if kw == "while":
                return self.parse_while()
            if kw == "do":
                self.next()
                body = self.parse_block()
                self.expect_kw("end")
                return self._mark(A.Do(body), t)
            if kw == "for":
                return self.parse_for()
            if kw == "repeat":
                return self.parse_repeat()
            if kw == "function":
                return self.parse_function_stat()
            if kw == "local":
                return self.parse_local()
            if kw == "goto":
                self.next()
                name = self.expect_name().value
                return self._mark(A.Goto(name), t)
            if kw in ("end", "else", "elseif", "until"):
                self.error("unexpected symbol")

        return self.parse_expr_stat()

    # -- Luau types --------------------------------------------------------

    def parse_type_decl(self, is_export: bool) -> A.Stat:
        t = self.next()  # the contextual `type` keyword
        name = self.expect_name().value
        if self.check_op("<"):
            self.skip_generic_params()
        self.expect_op("=")
        self.skip_type()
        return self._mark(A.TypeDecl(name, is_export), t)

    def skip_generic_params(self) -> None:
        """Consume ``<T, U...>`` after a function or type name."""
        depth = 0
        while True:
            tok = self.tok
            if tok.kind == EOF:
                self.error("unfinished generic parameter list")
            if tok.kind == OP and tok.value == "<":
                depth += 1
                self.next()
                continue
            if tok.kind == OP and tok.value == ">":
                depth -= 1
                self.next()
                if depth <= 0:
                    return
                continue
            if tok.kind == OP and tok.value == ">>":
                # ``Array<Array<T>>`` lexes as ``>>``
                depth -= 2
                self.next()
                if depth <= 0:
                    return
                continue
            self.next()

    def skip_type(self) -> None:
        """Consume a type expression without building a node for it.

        Types have no runtime effect, so the obfuscator only needs to find where
        a type ends.  The heuristic: consume tokens while they can still be part
        of the type (depth-aware); stop at ``=``, ``,`` or a closing bracket at
        depth 0, or when a new atom follows a complete atom with no type
        operator in between.
        """
        depth = 0
        have_atom = False
        while True:
            tok = self.tok
            if tok.kind == EOF:
                self.error("unfinished type annotation")
            is_op = tok.kind == OP
            value = tok.value if is_op else None

            if is_op and value in ("(", "{", "["):
                depth += 1
                have_atom = False
                self.next()
                continue
            if is_op and value in (")", "}", "]"):
                if depth == 0:
                    return
                depth -= 1
                have_atom = True
                self.next()
                continue
            if depth == 0:
                if is_op and value in ("=", ","):
                    return
                if is_op and value == ">":
                    return
                if have_atom and not (is_op and value in TYPE_CONTINUATIONS):
                    return
            if is_op and value == "<":
                # generic arguments: Array<number>
                self.skip_generic_params()
                have_atom = True
                continue
            if is_op and value == "->":
                self.next()
                have_atom = False
                continue
            self.next()
            have_atom = not (is_op and value in ("|", "&", "?", "."))

    def parse_if(self) -> A.If:
        t = self.expect_kw("if")
        clauses: list[tuple[A.Expr, A.Block]] = []
        cond = self.parse_expr()
        self.expect_kw("then")
        clauses.append((cond, self.parse_block()))
        orelse = None
        while True:
            if self.accept_kw("elseif"):
                c = self.parse_expr()
                self.expect_kw("then")
                clauses.append((c, self.parse_block()))
                continue
            if self.accept_kw("else"):
                orelse = self.parse_block()
                self.expect_kw("end")
                break
            self.expect_kw("end")
            break
        return self._mark(A.If(clauses, orelse), t)

    def parse_while(self) -> A.While:
        t = self.expect_kw("while")
        cond = self.parse_expr()
        self.expect_kw("do")
        body = self.parse_block()
        self.expect_kw("end")
        return self._mark(A.While(cond, body), t)

    def parse_repeat(self) -> A.Repeat:
        t = self.expect_kw("repeat")
        body = self.parse_block()
        self.expect_kw("until")
        cond = self.parse_expr()
        return self._mark(A.Repeat(body, cond), t)

    def parse_for(self) -> A.Stat:
        t = self.expect_kw("for")
        first = self.expect_name().value

        if self.accept_op("="):
            start = self.parse_expr()
            self.expect_op(",")
            stop = self.parse_expr()
            step = None
            if self.accept_op(","):
                step = self.parse_expr()
            self.expect_kw("do")
            body = self.parse_block()
            self.expect_kw("end")
            return self._mark(A.NumFor(first, start, stop, step, body), t)

        names = [first]
        while self.accept_op(","):
            names.append(self.expect_name().value)
        self.expect_kw("in")
        exprs = self.parse_exprlist()
        self.expect_kw("do")
        body = self.parse_block()
        self.expect_kw("end")
        return self._mark(A.GenFor(names, exprs, body), t)

    def parse_function_stat(self) -> A.FuncStat:
        t = self.expect_kw("function")
        path = [self.expect_name().value]
        while self.accept_op("."):
            path.append(self.expect_name().value)
        method = None
        if self.accept_op(":"):
            method = self.expect_name().value
        func = self.parse_function_body(is_method=method is not None)
        return self._mark(A.FuncStat(path, method, func), t)

    def parse_local(self) -> A.Stat:
        t = self.expect_kw("local")
        if self.accept_kw("function"):
            name = self.expect_name().value
            func = self.parse_function_body(is_method=False)
            return self._mark(A.LocalFunction(name, func), t)

        names: list[str] = []
        attribs: list[str | None] = []
        while True:
            names.append(self.expect_name().value)
            attr = None
            if self.accept_op("<"):
                attr = self.expect_name().value
                self.expect_op(">")
            attribs.append(attr)
            if self.luau and self.check_op(":"):
                self.next()
                self.skip_type()
            if not self.accept_op(","):
                break
        exprs: list[A.Expr] = []
        if self.accept_op("="):
            exprs = self.parse_exprlist()
        return self._mark(A.LocalDecl(names, exprs, attribs), t)

    def parse_expr_stat(self) -> A.Stat:
        t = self.tok
        first = self.parse_suffixed_expr()

        if self.luau and self.tok.kind == OP and self.tok.value in COMPOUND_OPS:
            return self.parse_compound_assign(first, t)

        if self.check_op("=") or self.check_op(","):
            targets = [first]
            while self.accept_op(","):
                targets.append(self.parse_suffixed_expr())
            self.expect_op("=")
            exprs = self.parse_exprlist()
            for target in targets:
                if not isinstance(target, (A.Name, A.Index)):
                    self.error("syntax error (cannot assign to this expression)", t)
            return self._mark(A.Assign(targets, exprs), t)

        if not isinstance(first, (A.Call, A.MethodCall)):
            self.error("syntax error (unexpected expression statement)", t)
        return self._mark(A.CallStat(first), t)

    def parse_compound_assign(self, target: A.Expr, t: Token) -> A.Stat:
        """Lower Luau ``x op= v`` into an ordinary assignment.

        Luau evaluates the table and key of ``a[b] op= v`` exactly once, so when
        the target has side effects we park the receiver and key in temporaries
        first.  The whole thing is wrapped in ``do … end`` so the temporaries
        cannot leak into the surrounding scope.
        """
        op = COMPOUND_OPS[self.next().value]
        value = self.parse_expr()
        if not isinstance(target, (A.Name, A.Index)):
            self.error("cannot assign to this expression", t)

        if isinstance(target, A.Index) and _is_pure(target.obj) and _is_pure(target.key):
            return self._mark(
                A.Assign([target], [A.BinOp(op, _clone(target), value)]), t
            )
        if isinstance(target, A.Name):
            return self._mark(A.Assign([target], [A.BinOp(op, A.Name(target.id), value)]), t)

        # general case: evaluate receiver/key once
        stats: list[A.Stat] = []
        obj_temp = self.fresh_local("o")
        stats.append(A.LocalDecl([obj_temp], [target.obj]))
        if isinstance(target.key, A.String) and _is_identifier_key(target.key):
            read = A.Index(A.Name(obj_temp), A.String(target.key.value))
            new_value = A.BinOp(op, read, value)
            stats.append(A.Assign([read], [new_value]))
            # A.Index node reused for read and write: clone the key for safety
            stats[1].targets[0] = A.Index(A.Name(obj_temp), A.String(target.key.value))
        else:
            key_temp = self.fresh_local("k")
            stats.append(A.LocalDecl([key_temp], [target.key]))
            read = A.Index(A.Name(obj_temp), A.Name(key_temp))
            stats.append(A.Assign([A.Index(A.Name(obj_temp), A.Name(key_temp))], [A.BinOp(op, read, value)]))
        return self._mark(A.Do(A.Block(stats)), t)

    # -- functions ---------------------------------------------------------

    def parse_function_body(self, is_method: bool) -> A.Function:
        if self.luau and self.check_op("<"):
            self.skip_generic_params()
        self.expect_op("(")
        params: list[A.Param] = []
        is_vararg = False
        if not self.check_op(")"):
            while True:
                if self.check_op("..."):
                    self.next()
                    if self.luau and self.check_op(":"):
                        self.next()
                        self.skip_type()
                    is_vararg = True
                    break
                params.append(A.Param(self.expect_name().value))
                if self.luau and self.check_op(":"):
                    self.next()
                    self.skip_type()
                if not self.accept_op(","):
                    break
        self.expect_op(")")
        if self.luau and self.check_op(":"):
            self.next()
            self.skip_type()
        if is_method:
            params.insert(0, A.Param("self"))
        body = self.parse_block()
        self.expect_kw("end")
        return A.Function(params, is_vararg, body, is_method)

    # -- expressions -------------------------------------------------------

    def parse_exprlist(self) -> list[A.Expr]:
        exprs = [self.parse_expr()]
        while self.accept_op(","):
            exprs.append(self.parse_expr())
        return exprs

    def parse_expr(self, limit: int = 0) -> A.Expr:
        t = self.tok
        if (t.kind == KEYWORD and t.value in ("not",)) or (
            t.kind == OP and t.value in ("-", "#", "~")
        ):
            op = t.value
            self.next()
            operand = self.parse_expr(UNARY_PRIORITY)
            left: A.Expr = self._mark(A.UnOp(op, operand), t)
        else:
            left = self.parse_simple_expr()

        while True:
            t = self.tok
            if t.kind == KEYWORD and t.value in BINARY_PRIORITY:
                op = t.value
            elif t.kind == OP and t.value in BINARY_PRIORITY:
                op = t.value
            else:
                break
            lprec, rprec = BINARY_PRIORITY[op]
            if lprec < limit:
                break
            self.next()
            right = self.parse_expr(rprec)
            left = self._mark(A.BinOp(op, left, right), t)
        return left

    def parse_simple_expr(self) -> A.Expr:
        t = self.tok
        if t.kind == NUMBER:
            self.next()
            value, is_int = t.value
            return self._mark(A.Number(value, t.raw, is_int), t)
        if t.kind == STRING:
            self.next()
            return self._mark(A.String(t.value, t.raw), t)
        if t.kind == KEYWORD:
            if t.value == "if" and self.luau:
                return self.parse_if_expr()
            if t.value == "nil":
                self.next()
                return self._mark(A.Nil(), t)
            if t.value == "true":
                self.next()
                return self._mark(A.TrueExpr(), t)
            if t.value == "false":
                self.next()
                return self._mark(A.FalseExpr(), t)
            if t.value == "function":
                self.next()
                return self._mark(self.parse_function_body(is_method=False), t)
        if self.check_op("..."):
            self.next()
            return self._mark(A.Vararg(), t)
        if self.check_op("{"):
            return self.parse_table()
        return self.parse_suffixed_expr()

    def parse_if_expr(self) -> A.Expr:
        """Luau ``if c then a elseif d then b else e`` used as an expression."""
        t = self.expect_kw("if")
        clauses: list[tuple[A.Expr, A.Expr]] = []
        cond = self.parse_expr()
        self.expect_kw("then")
        clauses.append((cond, self.parse_expr()))
        while self.accept_kw("elseif"):
            c = self.parse_expr()
            self.expect_kw("then")
            clauses.append((c, self.parse_expr()))
        self.expect_kw("else")
        orelse = self.parse_expr()
        return self._mark(A.IfExpr(clauses, orelse), t)

    def parse_primary_expr(self) -> A.Expr:
        t = self.tok
        if t.kind == NAME:
            self.next()
            return self._mark(A.Name(t.value), t)
        if self.check_op("("):
            self.next()
            expr = self.parse_expr()
            self.expect_op(")")
            return self._mark(A.Paren(expr), t)
        self.error("unexpected symbol")

    def parse_suffixed_expr(self) -> A.Expr:
        expr = self.parse_primary_expr()
        while True:
            t = self.tok
            if self.check_op("."):
                self.next()
                key_tok = self.expect_name()
                expr = self._mark(A.Index(expr, A.String(key_tok.value.encode())), t)
                continue
            if self.check_op("["):
                self.next()
                key = self.parse_expr()
                self.expect_op("]")
                expr = self._mark(A.Index(expr, key), t)
                continue
            if self.check_op(":"):
                self.next()
                name = self.expect_name().value
                args = self.parse_call_args()
                expr = self._mark(A.MethodCall(expr, name, args), t)
                continue
            if self.luau and self.check_op("::"):
                # Luau type cast: the annotation is erased, the value is kept
                self.next()
                self.skip_type()
                continue
            if self.check_op("(", "{"):
                args = self.parse_call_args()
                expr = self._mark(A.Call(expr, args), t)
                continue
            if self.tok.kind == STRING:
                args = self.parse_call_args()
                expr = self._mark(A.Call(expr, args), t)
                continue
            return expr

    def parse_call_args(self) -> list[A.Expr]:
        t = self.tok
        if t.kind == STRING:
            self.next()
            return [self._mark(A.String(t.value, t.raw), t)]
        if self.check_op("{"):
            return [self.parse_table()]
        self.expect_op("(")
        if self.check_op(")"):
            self.next()
            return []
        args = self.parse_exprlist()
        self.expect_op(")")
        return args

    def parse_table(self) -> A.Table:
        t = self.expect_op("{")
        fields: list[A.TableField] = []
        while not self.check_op("}"):
            if self.check_op("["):
                self.next()
                key = self.parse_expr()
                self.expect_op("]")
                self.expect_op("=")
                value = self.parse_expr()
                fields.append(A.TableField(key, value))
            elif self.tok.kind == NAME and self.lookahead().kind == OP and self.lookahead().value == "=":
                key_tok = self.next()
                self.expect_op("=")
                value = self.parse_expr()
                fields.append(A.TableField(A.String(key_tok.value.encode()), value))
            else:
                fields.append(A.TableField(None, self.parse_expr()))
            if not (self.accept_op(",") or self.accept_op(";")):
                break
        self.expect_op("}")
        return self._mark(A.Table(fields), t)


def _is_pure(node: A.Expr) -> bool:
    """Can this expression be evaluated twice with no observable difference?"""
    if isinstance(node, (A.Name, A.Nil, A.TrueExpr, A.FalseExpr, A.Number, A.String, A.Vararg)):
        return True
    if isinstance(node, A.Index):
        return _is_pure(node.obj) and _is_pure(node.key)
    return False


def _is_identifier_key(node: A.Expr) -> bool:
    from .unparser import is_identifier

    return isinstance(node, A.String) and is_identifier(node.text())


def _clone(node: A.Expr) -> A.Expr:
    """Shallow clone for simple expressions (used by compound assignment)."""
    import copy

    return copy.deepcopy(node)


def _parse_with(source: str, filename: str | None, dialect: str) -> A.Chunk:
    tokens = Lexer(source, filename, dialect).tokenize()
    chunk = Parser(tokens, filename, dialect).parse_chunk()
    chunk.dialect = dialect
    return chunk


def parse(source: str, filename: str | None = None, dialect: str = "auto") -> A.Chunk:
    """Parse Lua ``source`` into a :class:`luaobf.ast_nodes.Chunk`.

    ``dialect`` is ``"lua"`` (strict Lua 5.1–5.4), ``"luau"`` (adds Roblox/Luau
    syntax) or ``"auto"`` (try strict first, then Luau).
    """
    if dialect not in ("lua", "luau", "auto"):
        raise ValueError(f"unknown dialect {dialect!r}")
    if dialect != "auto":
        return _parse_with(source, filename, dialect)
    try:
        return _parse_with(source, filename, "lua")
    except LuaSyntaxError as strict_error:
        try:
            return _parse_with(source, filename, "luau")
        except LuaSyntaxError as luau_error:
            # report whichever error got further into the file
            first = (strict_error.line, strict_error.col)
            second = (luau_error.line, luau_error.col)
            raise (luau_error if second > first else strict_error)
