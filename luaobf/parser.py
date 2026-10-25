"""Recursive-descent parser producing the luaobf AST.

Covers the full Lua 5.1 – 5.4 statement/expression grammar: numeric and generic
``for``, ``goto``/labels, method definitions and calls, varargs, table
constructors with all three field forms, 5.4 ``<const>``/``<close>`` attributes
and the 5.3 bitwise operators.
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


class Parser:
    def __init__(self, tokens: list[Token], filename: str | None = None) -> None:
        self.tokens = tokens
        self.pos = 0
        self.filename = filename

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
            if not self.accept_op(","):
                break
        exprs: list[A.Expr] = []
        if self.accept_op("="):
            exprs = self.parse_exprlist()
        return self._mark(A.LocalDecl(names, exprs, attribs), t)

    def parse_expr_stat(self) -> A.Stat:
        t = self.tok
        first = self.parse_suffixed_expr()
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

    # -- functions ---------------------------------------------------------

    def parse_function_body(self, is_method: bool) -> A.Function:
        self.expect_op("(")
        params: list[A.Param] = []
        is_vararg = False
        if not self.check_op(")"):
            while True:
                if self.check_op("..."):
                    self.next()
                    is_vararg = True
                    break
                params.append(A.Param(self.expect_name().value))
                if not self.accept_op(","):
                    break
        self.expect_op(")")
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


def parse(source: str, filename: str | None = None) -> A.Chunk:
    """Parse Lua ``source`` into a :class:`luaobf.ast_nodes.Chunk`."""
    tokens = Lexer(source, filename).tokenize()
    return Parser(tokens, filename).parse_chunk()
