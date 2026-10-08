from __future__ import annotations

import re
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple, Union


class PacSyntaxError(ValueError):
    """Raised when the PAC script text cannot be parsed."""


class PacRuntimeError(RuntimeError):
    """Raised when evaluation of a parsed PAC hits an unsupported construct
    or a type error within the script's own logic."""


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindProxyForURLResult:
    """Result of calling ``FindProxyForURL``.

    ``value`` is the raw string returned by the script. ``direct`` and
    ``proxies`` are parsed conveniences for the common ``DIRECT`` / ``PROXY
    host:port`` / ``SOCKS host:port`` tokens.
    """

    value: str
    direct: bool
    proxies: Sequence[str]


class Proxies:
    """Namespace of well-known PAC return tokens."""

    DIRECT = "DIRECT"
    PROXY = "PROXY"
    SOCKS = "SOCKS"
    SOCKS4 = "SOCKS4"
    SOCKS5 = "SOCKS5"
    HTTPS = "HTTPS"


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<line_comment>//[^\n]*)
    | (?P<block_comment>/\*.*?\*/)
    | (?P<number>(?:\d+\.\d+|\.\d+|\d+\.)(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)
    | (?P<string>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
    | (?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)
    | (?P<punct>[(){};,.[\]:?])
    | (?P<op>(?:&&|\|\||===|!==|==|!=|<=|>=|<<|>>|\+\+|--|\+=|-=|\*=|/=|%=|[+\-*/%<>&|!~=^]))
    """,
    re.DOTALL | re.VERBOSE,
)

_PUNCTUATORS = {
    "(", ")", "{", "}", ";", ",", ".", "[", "]", ":", "?",
}


_TOKEN_KEYWORDS = ("var", "function", "return", "if", "else", "new")


class Token:
    __slots__ = ("type", "value", "pos")

    def __init__(self, type_: str, value: str, pos: int) -> None:
        self.type = type_
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:
        return f"Token({self.type!r}, {self.value!r}, pos={self.pos})"


def tokenize(src: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(src)
    while i < n:
        m = _TOKEN_RE.match(src, i)
        if not m:
            line = src.count("\n", 0, i) + 1
            col = i - (src.rfind("\n", 0, i) + 1) + 1
            raise PacSyntaxError(
                f"Unexpected character {src[i]!r} at line {line}, column {col}"
            )
        i = m.end()
        kind = m.lastgroup
        if kind in ("ws", "line_comment", "block_comment"):
            continue
        if kind == "punct":
            tokens.append(Token("punct", m.group(), m.start()))
        elif kind == "op":
            tokens.append(Token("op", m.group(), m.start()))
        elif kind == "number":
            tokens.append(Token("number", m.group(), m.start()))
        elif kind == "string":
            tokens.append(Token("string", m.group(), m.start()))
        elif kind == "ident":
            v = m.group()
            tokens.append(Token("keyword", v, m.start()) if v in _TOKEN_KEYWORDS else Token("ident", v, m.start()))
    tokens.append(Token("eof", "", n))
    return tokens


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


class Node:
    __slots__ = ()


class Program(Node):
    __slots__ = ("body",)

    def __init__(self, body: List[Node]) -> None:
        self.body = body


class VarDecl(Node):
    __slots__ = ("name", "value")

    def __init__(self, name: str, value: Optional[Node]) -> None:
        self.name = name
        self.value = value


class FunctionDecl(Node):
    __slots__ = ("name", "params", "body")

    def __init__(self, name: str, params: List[str], body: List[Node]) -> None:
        self.name = name
        self.params = params
        self.body = body


class ReturnStmt(Node):
    __slots__ = ("value",)

    def __init__(self, value: Optional[Node]) -> None:
        self.value = value


class IfStmt(Node):
    __slots__ = ("test", "consequent", "alternate")

    def __init__(self, test: Node, consequent: List[Node], alternate: Optional[List[Node]]) -> None:
        self.test = test
        self.consequent = consequent
        self.alternate = alternate


class Block(Node):
    __slots__ = ("body",)

    def __init__(self, body: List[Node]) -> None:
        self.body = body


class ExprStmt(Node):
    __slots__ = ("expr",)

    def __init__(self, expr: Node) -> None:
        self.expr = expr


class Literal(Node):
    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value


class Identifier(Node):
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class ArrayLiteral(Node):
    __slots__ = ("elements",)

    def __init__(self, elements: List[Node]) -> None:
        self.elements = elements


class Call(Node):
    __slots__ = ("callee", "args")

    def __init__(self, callee: Node, args: List[Node]) -> None:
        self.callee = callee
        self.args = args


class Member(Node):
    __slots__ = ("obj", "prop", "computed")

    def __init__(self, obj: Node, prop: Node, computed: bool) -> None:
        self.obj = obj
        self.prop = prop
        self.computed = computed


class Unary(Node):
    __slots__ = ("op", "arg")

    def __init__(self, op: str, arg: Node) -> None:
        self.op = op
        self.arg = arg


class Binary(Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: Node, right: Node) -> None:
        self.op = op
        self.left = left
        self.right = right


class Logical(Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: Node, right: Node) -> None:
        self.op = op
        self.left = left
        self.right = right


class Conditional(Node):
    __slots__ = ("test", "consequent", "alternate")

    def __init__(self, test: Node, consequent: Node, alternate: Node) -> None:
        self.test = test
        self.consequent = consequent
        self.alternate = alternate


class Assign(Node):
    __slots__ = ("op", "target", "value")

    def __init__(self, op: str, target: Node, value: Node) -> None:
        self.op = op
        self.target = target
        self.value = value


# ---------------------------------------------------------------------------
# Parser — recursive descent for the subset we support.
# ---------------------------------------------------------------------------

_PRECEDENCE = [
    (["||"], "logical-or"),
    (["&&"], "logical-and"),
    (["|"], "bitor"),
    (["^"], "bitxor"),
    (["&"], "bitand"),
    (["==", "!=", "===", "!=="], "equality"),
    (["<", "<=", ">", ">="], "relational"),
    (["<<", ">>"], "shift"),
    (["+", "-"], "additive"),
    (["*", "/", "%"], "multiplicative"),
]


class _Parser:
    def __init__(self, src: str) -> None:
        self.src = src
        self.tokens = tokenize(src)
        self.i = 0

    # -- token helpers -----------------------------------------------------

    def _peek(self, off: int = 0) -> Token:
        idx = self.i + off
        if idx >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[idx]

    def _next(self) -> Token:
        t = self.tokens[self.i]
        if t.type != "eof":
            self.i += 1
        return t

    def _expect(self, type_: str, value: Optional[str] = None) -> Token:
        t = self._next()
        if t.type != type_ or (value is not None and t.value != value):
            raise PacSyntaxError(
                f"Expected {type_}{'/' + value if value else ''} but got {t.type}/{t.value!r}"
            )
        return t

    def _accept(self, type_: str, value: Optional[str] = None) -> Optional[Token]:
        t = self._peek()
        if t.type == type_ and (value is None or t.value == value):
            self.i += 1
            return t
        return None

    # -- top level ---------------------------------------------------------

    def parse(self) -> Program:
        body: List[Node] = []
        while self._peek().type != "eof":
            body.append(self._statement())
        return Program(body)

    def _statement(self) -> Node:
        t = self._peek()
        if t.type == "keyword" and t.value == "var":
            return self._var_decl()
        if t.type == "keyword" and t.value == "function":
            return self._function_decl()
        if t.type == "keyword" and t.value == "return":
            return self._return()
        if t.type == "keyword" and t.value == "if":
            return self._if()
        if t.type == "keyword" and t.value == "new":
            raise PacSyntaxError("'new' is not supported")
        if t.type == "punct" and t.value == "{":
            return Block(self._block_body())
        # expression statement, optional semicolon
        expr = self._expression()
        self._accept("punct", ";")
        return ExprStmt(expr)

    def _var_decl(self) -> VarDecl:
        self._expect("keyword", "var")
        name = self._expect("ident").value
        value: Optional[Node] = None
        if self._accept("op", "="):
            value = self._assignment_expr()
        self._accept("punct", ";")
        return VarDecl(name, value)

    def _function_decl(self) -> FunctionDecl:
        self._expect("keyword", "function")
        name = self._expect("ident").value
        self._expect("punct", "(")
        params: List[str] = []
        if self._peek().type != "punct" or self._peek().value != ")":
            params.append(self._expect("ident").value)
            while self._accept("punct", ","):
                params.append(self._expect("ident").value)
        self._expect("punct", ")")
        body = self._block_body()
        self._expect("punct", "}")
        return FunctionDecl(name, params, body)

    def _return(self) -> ReturnStmt:
        self._expect("keyword", "return")
        if self._peek().type == "punct" and self._peek().value == ";":
            self._next()
            return ReturnStmt(None)
        if self._peek().type == "punct" and self._peek().value == "}":
            return ReturnStmt(None)
        expr = self._expression()
        self._accept("punct", ";")
        return ReturnStmt(expr)

    def _if(self) -> IfStmt:
        self._expect("keyword", "if")
        self._expect("punct", "(")
        test = self._expression()
        self._expect("punct", ")")
        consequent = self._block_or_stmt()
        alternate: Optional[List[Node]] = None
        if self._peek().type == "keyword" and self._peek().value == "else":
            self._next()
            if self._peek().type == "keyword" and self._peek().value == "if":
                alternate = [self._if()]
            else:
                alternate = self._block_or_stmt()
        return IfStmt(test, consequent, alternate)

    def _block_or_stmt(self) -> List[Node]:
        if self._peek().type == "punct" and self._peek().value == "{":
            body = self._block_body()
            self._expect("punct", "}")
            return body
        return [self._statement()]

    def _block_body(self) -> List[Node]:
        self._expect("punct", "{")
        body: List[Node] = []
        while not (self._peek().type == "punct" and self._peek().value == "}"):
            if self._peek().type == "eof":
                raise PacSyntaxError("Unterminated block")
            body.append(self._statement())
        return body

    # -- expressions -------------------------------------------------------

    def _expression(self) -> Node:
        return self._assignment_expr()

    def _assignment_expr(self) -> Node:
        left = self._conditional()
        t = self._peek()
        if t.type == "op" and t.value in ("=", "+=", "-=", "*=", "/=", "%="):
            self._next()
            right = self._assignment_expr()
            return Assign(t.value, left, right)
        return left

    def _conditional(self) -> Node:
        test = self._binary(0)
        if self._accept("punct", "?"):
            consequent = self._assignment_expr()
            self._expect("punct", ":")
            alternate = self._assignment_expr()
            return Conditional(test, consequent, alternate)
        return test

    def _binary(self, level: int) -> Node:
        if level >= len(_PRECEDENCE):
            return self._unary()
        ops, _ = _PRECEDENCE[level]
        left = self._binary(level + 1)
        while True:
            t = self._peek()
            if t.type == "op" and t.value in ops:
                self._next()
                right = self._binary(level + 1)
                if t.value in ("&&", "||"):
                    left = Logical(t.value, left, right)
                else:
                    left = Binary(t.value, left, right)
            else:
                return left

    def _unary(self) -> Node:
        t = self._peek()
        if t.type == "op" and t.value in ("!", "-", "+", "~"):
            self._next()
            return Unary(t.value, self._unary())
        if t.type == "op" and t.value in ("++", "--"):
            # Pre-increment: desugar to a compound assignment.
            self._next()
            operand = self._unary()
            return Assign(t.value[0] + "=", operand, Literal(1))
        return self._postfix()

    def _postfix(self) -> Node:
        expr = self._call_member()
        t = self._peek()
        if t.type == "op" and t.value in ("++", "--"):
            self._next()
            return Assign(t.value[0] + "=", expr, Literal(1))
        return expr

    def _call_member(self) -> Node:
        expr = self._primary()
        while True:
            t = self._peek()
            if t.type == "punct" and t.value == ".":
                self._next()
                prop = self._expect("ident")
                expr = Member(expr, Literal(prop.value), computed=False)
            elif t.type == "punct" and t.value == "[":
                self._next()
                prop = self._expression()
                self._expect("punct", "]")
                expr = Member(expr, prop, computed=True)
            elif t.type == "punct" and t.value == "(":
                args = self._args()
                expr = Call(expr, args)
            else:
                return expr

    def _args(self) -> List[Node]:
        self._expect("punct", "(")
        args: List[Node] = []
        if not (self._peek().type == "punct" and self._peek().value == ")"):
            args.append(self._assignment_expr())
            while self._accept("punct", ","):
                args.append(self._assignment_expr())
        self._expect("punct", ")")
        return args

    def _primary(self) -> Node:
        t = self._peek()
        if t.type == "number":
            self._next()
            v = t.value
            if "." in v or "e" in v or "E" in v:
                return Literal(float(v))
            return Literal(int(v))
        if t.type == "string":
            self._next()
            return Literal(_decode_string_literal(t.value))
        if t.type == "ident":
            self._next()
            if t.value == "true":
                return Literal(True)
            if t.value == "false":
                return Literal(False)
            if t.value == "null":
                return Literal(None)
            if t.value == "undefined":
                return Literal(None)
            return Identifier(t.value)
        if t.type == "punct" and t.value == "(":
            self._next()
            e = self._expression()
            self._expect("punct", ")")
            return e
        if t.type == "punct" and t.value == "[":
            self._next()
            elements: List[Node] = []
            if not (self._peek().type == "punct" and self._peek().value == "]"):
                elements.append(self._assignment_expr())
                while self._accept("punct", ","):
                    if self._peek().type == "punct" and self._peek().value == "]":
                        break
                    elements.append(self._assignment_expr())
            self._expect("punct", "]")
            return ArrayLiteral(elements)
        raise PacSyntaxError(f"Unexpected token {t.type}/{t.value!r} in primary expression")


def _decode_string_literal(raw: str) -> str:
    quote = raw[0]
    body = raw[1:-1]
    out: List[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(body):
            out.append("\\")
            break
        e = body[i]
        i += 1
        if e == "n":
            out.append("\n")
        elif e == "t":
            out.append("\t")
        elif e == "r":
            out.append("\r")
        elif e == "b":
            out.append("\b")
        elif e == "f":
            out.append("\f")
        elif e == "v":
            out.append("\v")
        elif e == "0":
            out.append("\0")
        elif e == "x":
            if i + 1 < len(body):
                hexs = body[i:i + 2]
                i += 2
                out.append(chr(int(hexs, 16)))
            else:
                out.append("x")
        elif e == "u":
            if i + 3 < len(body):
                hexs = body[i:i + 4]
                i += 4
                out.append(chr(int(hexs, 16)))
            else:
                out.append("u")
        elif e == "\n":
            pass
        elif e == "\\":
            out.append("\\")
        elif e == "'":
            out.append("'")
        elif e == '"':
            out.append('"')
        else:
            out.append(e)
    return "".join(out)


# ---------------------------------------------------------------------------
# Runtime values
# ---------------------------------------------------------------------------


class _Undefined:
    _instance: Optional["_Undefined"] = None

    def __new__(cls) -> "_Undefined":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "undefined"

    def __bool__(self) -> bool:
        return False


_UNDEFINED = _Undefined()


class _JSArray:
    def __init__(self, items: List[object]) -> None:
        self._items = items

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, idx: int) -> object:
        return self._items[idx]


class _JSFunction:
    def __init__(self, params: List[str], body: List[Node], closure: "Environment") -> None:
        self.params = params
        self.body = body
        self.closure = closure


class _ReturnSignal(Exception):
    def __init__(self, value: object) -> None:
        self.value = value


_UNARY_OPS = {
    "!": lambda x: not _truthy(x),
    "-": lambda x: -_to_number(x),
    "+": lambda x: _to_number(x),
    "~": lambda x: ~_to_int32(x),
}


def _truthy(v: object) -> bool:
    if v is _UNDEFINED or v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:  # NaN
            return False
        return v != 0
    if isinstance(v, str):
        return len(v) > 0
    return True


def _to_number(v: object) -> float:
    if v is _UNDEFINED or v is None:
        return float("nan") if v is _UNDEFINED else 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return 0.0
        try:
            return float(int(s, 0)) if s.lower().startswith("0x") else float(s)
        except ValueError:
            return float("nan")
    return float("nan")


def _to_int32(v: object) -> int:
    n = _to_number(v)
    if n != n or n in (float("inf"), float("-inf")):
        return 0
    return int(n) & 0xFFFFFFFF


def _to_string(v: object) -> str:
    if v is _UNDEFINED:
        return "undefined"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v != v:
            return "NaN"
        if v == float("inf"):
            return "Infinity"
        if v == float("-inf"):
            return "-Infinity"
        if v == int(v) and abs(v) < 1e21:
            return str(int(v))
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, _JSArray):
        return ",".join(_to_string(x) if x is not _UNDEFINED and x is not None else "" for x in v._items)
    return str(v)


def _binary_op(op: str, left: object, right: object) -> object:
    if op == "+":
        if isinstance(left, str) or isinstance(right, str):
            return _to_string(left) + _to_string(right)
        if isinstance(left, _JSArray) or isinstance(right, _JSArray):
            return _to_string(left) + _to_string(right)
        return _to_number(left) + _to_number(right)
    if op == "-":
        return _to_number(left) - _to_number(right)
    if op == "*":
        return _to_number(left) * _to_number(right)
    if op == "/":
        r = _to_number(right)
        l = _to_number(left)
        if r == 0:
            if l == 0:
                return float("nan")
            return float("inf") if l > 0 else float("-inf")
        return l / r
    if op == "%":
        l = _to_number(left)
        r = _to_number(right)
        if r == 0:
            return float("nan")
        if isinstance(l, int) and isinstance(r, int):
            # JS % keeps sign of dividend.
            return l - int(l / r) * r if r != 0 else float("nan")
        return l - int(l / r) * r
    if op in ("==", "===", "!=", "!=="):
        if op in ("==", "!="):
            eq = _loose_equals(left, right)
        else:
            eq = _strict_equals(left, right)
        return eq if op in ("==", "===") else not eq
    if op in ("<", "<=", ">", ">="):
        if isinstance(left, str) and isinstance(right, str):
            a, b = left, right
        else:
            a, b = _to_number(left), _to_number(right)
        try:
            if op == "<":
                return a < b
            if op == "<=":
                return a <= b
            if op == ">":
                return a > b
            return a >= b
        except TypeError:
            return False
    if op == "&":
        return _to_int32(left) & _to_int32(right)
    if op == "|":
        return _to_int32(left) | _to_int32(right)
    if op == "^":
        return _to_int32(left) ^ _to_int32(right)
    if op == "<<":
        return (_to_int32(left) << (_to_int32(right) & 31)) & 0xFFFFFFFF
    if op == ">>":
        return _to_int32(left) >> (_to_int32(right) & 31)
    raise PacRuntimeError(f"Unsupported binary operator {op!r}")


def _loose_equals(a: object, b: object) -> bool:
    if type(a) is type(b) or (a is None and b is _UNDEFINED) or (a is _UNDEFINED and b is None):
        return _strict_equals(a, b)
    if isinstance(a, (int, float)) and isinstance(b, str):
        return _to_number(a) == _to_number(b)
    if isinstance(a, str) and isinstance(b, (int, float)):
        return _to_number(a) == _to_number(b)
    if isinstance(a, bool):
        return _loose_equals(_to_number(a), b)
    if isinstance(b, bool):
        return _loose_equals(a, _to_number(b))
    return _strict_equals(a, b)


def _strict_equals(a: object, b: object) -> bool:
    if a is _UNDEFINED or b is _UNDEFINED:
        return a is b
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        if isinstance(a, bool) and isinstance(b, bool):
            return a == b
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return type(a) is type(b) and a == b if False else float(a) == float(b)
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a is b


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class Environment:
    def __init__(self, parent: Optional["Environment"] = None) -> None:
        self._vars: dict = {}
        self._parent = parent

    def get(self, name: str) -> object:
        env: Optional[Environment] = self
        while env is not None:
            if name in env._vars:
                return env._vars[name]
            env = env._parent
        return _UNDEFINED

    def set_existing(self, name: str, value: object) -> bool:
        env: Optional[Environment] = self
        while env is not None:
            if name in env._vars:
                env._vars[name] = value
                return True
            env = env._parent
        return False

    def declare(self, name: str, value: object) -> None:
        self._vars[name] = value


# ---------------------------------------------------------------------------
# PAC utility functions
# ---------------------------------------------------------------------------


def _dns_resolve(host: str) -> str:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return ""
    for info in infos:
        ip = info[4][0]
        if "." in ip or ":" in ip:
            return ip
    return ""


def _is_plain_ip(host: str) -> bool:
    return bool(re.match(r"^\d+\.\d+\.\d+\.\d+$", host))


def _cidr_match(ip: str, prefix: str) -> bool:
    try:
        network = urllib.parse.urlparse("//" + prefix).hostname or prefix
        if "/" in network:
            net_part, _, bits = network.partition("/")
            bits_i = int(bits)
        else:
            net_part = network
            bits_i = 32
        net_ip = net_part.split("/")[0]
    except Exception:
        return False
    try:
        ip_i = int.from_bytes(socket.inet_aton(ip), "big")
        net_i = int.from_bytes(socket.inet_aton(net_ip), "big")
    except OSError:
        return False
    mask = ((1 << bits_i) - 1) << (32 - bits_i) if 0 < bits_i <= 32 else 0xFFFFFFFF
    return (ip_i & mask) == (net_i & mask)


def _make_pac_globals(dns_resolver: Callable[[str], str]) -> dict:
    def shExpMatch(str_val: str, shexp: str) -> bool:
        regex = "^" + re.escape(shexp).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        return re.match(regex, str_val) is not None

    def isInNet(host: str, pattern: str, mask: str) -> bool:
        ip = dns_resolver(host) if not _is_plain_ip(host) else host
        try:
            ip_i = int.from_bytes(socket.inet_aton(ip), "big")
            pat_i = int.from_bytes(socket.inet_aton(pattern), "big")
            mask_i = int.from_bytes(socket.inet_aton(mask), "big")
        except OSError:
            return False
        return (ip_i & mask_i) == (pat_i & mask_i)

    def dnsResolve(host: str) -> str:
        return dns_resolver(host)

    def myIpAddress() -> str:
        return dns_resolver(socket.gethostname())

    def dnsDomainIs(host: str, domain: str) -> bool:
        if domain.startswith("."):
            return host == domain.lstrip(".") or host.endswith(domain)
        return host == domain or host.endswith("." + domain)

    def localHostOrDomainIs(host: str, hostdom: str) -> bool:
        if host == hostdom:
            return True
        if "." in host:
            return False
        return hostdom.startswith(host + ".")

    def isResolvable(host: str) -> bool:
        return dns_resolver(host) != ""

    def isPlainHostName(host: str) -> bool:
        return "." not in host and ":" not in host

    def convert_addr(ip: str) -> int:
        return int.from_bytes(socket.inet_aton(ip), "big")

    def dnsDomainLevels(host: str) -> int:
        if not host:
            return 0
        return host.count(".")

    def alert(_msg: object) -> _Undefined:
        return _UNDEFINED

    def console_log_array(items: List[object]) -> str:
        return " ".join(_to_string(x) for x in items)

    return {
        "shExpMatch": shExpMatch,
        "isInNet": isInNet,
        "dnsResolve": dnsResolve,
        "myIpAddress": myIpAddress,
        "dnsDomainIs": dnsDomainIs,
        "localHostOrDomainIs": localHostOrDomainIs,
        "isResolvable": isResolvable,
        "isPlainHostName": isPlainHostName,
        "isInNet": isInNet,
        "dnsDomainLevels": dnsDomainLevels,
        "alert": alert,
        "console": type("Console", (), {"log": staticmethod(lambda *a: _UNDEFINED)})(),
        "String": lambda v=None: "" if v is None or v is _UNDEFINED else _to_string(v),
        "Number": _to_number,
        "Boolean": lambda v=False: _truthy(v),
        "parseInt": lambda s, r=10: _parse_int(s, r),
        "parseFloat": lambda s: _parse_float(s),
        "isNaN": lambda v: (lambda n: n != n)(_to_number(v)),
        "Math": _make_math(),
        "Array": type("Array", (), {"isArray": staticmethod(lambda v: isinstance(v, _JSArray))})(),
    }


def _parse_int(s: object, radix: object = 10) -> float:
    if isinstance(s, (int, float)):
        return float(int(s))
    if not isinstance(s, str):
        s = _to_string(s)
    s = s.strip()
    if not s:
        return float("nan")
    r = _to_number(radix)
    r_i = int(r) if r == r and r != 0 else 10
    if r_i == 0:
        r_i = 10
    sign = 1
    i = 0
    if s[0] in "+-":
        if s[0] == "-":
            sign = -1
        i = 1
    if r_i == 16 and i + 1 < len(s) and s[i] == "0" and s[i + 1] in "xX":
        i += 2
    elif r_i == 10 and i + 1 < len(s) and s[i] == "0" and i + 1 < len(s) and s[i + 1] in "xX":
        r_i = 16
        i += 2
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"[:r_i]
    j = i
    while j < len(s) and s[j].lower() in digits:
        j += 1
    if j == i:
        return float("nan")
    return float(sign * int(s[i:j], r_i))


def _parse_float(s: object) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str):
        s = _to_string(s)
    s = s.strip()
    if not s:
        return float("nan")
    m = re.match(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", s)
    if not m:
        return float("nan")
    try:
        return float(m.group(0))
    except ValueError:
        return float("nan")


def _make_math() -> object:
    import math

    return type("Math", (), {
        "floor": staticmethod(lambda x: float(math.floor(_to_number(x)))),
        "ceil": staticmethod(lambda x: float(math.ceil(_to_number(x)))),
        "round": staticmethod(lambda x: float(math.floor(_to_number(x) + 0.5))),
        "abs": staticmethod(lambda x: abs(_to_number(x))),
        "max": staticmethod(lambda *a: max((_to_number(x) for x in a), default=float("-inf"))),
        "min": staticmethod(lambda *a: min((_to_number(x) for x in a), default=float("inf"))),
        "pow": staticmethod(lambda x, y: float(_to_number(x) ** _to_number(y))),
        "sqrt": staticmethod(lambda x: math.sqrt(_to_number(x)) if _to_number(x) >= 0 else float("nan")),
        "random": staticmethod(lambda: 0.5),
        "PI": math.pi,
        "E": math.e,
    })()


# ---------------------------------------------------------------------------
# Interpreter
# ---------------------------------------------------------------------------


class Interpreter:
    def __init__(self, program: Program, globals_: dict) -> None:
        self.program = program
        self.global_env = Environment()
        self.functions: dict = {}
        for name, val in globals_.items():
            self.global_env.declare(name, val)
        for stmt in program.body:
            if isinstance(stmt, VarDecl):
                self.global_env.declare(stmt.name, _UNDEFINED)
        for stmt in program.body:
            if isinstance(stmt, FunctionDecl):
                self.functions[stmt.name] = _JSFunction(stmt.params, stmt.body, self.global_env)
                self.global_env.declare(stmt.name, self.functions[stmt.name])
        for stmt in program.body:
            if isinstance(stmt, VarDecl):
                self.global_env.declare(stmt.name, self._eval(stmt.value, self.global_env) if stmt.value else _UNDEFINED)

    def call_function(self, name: str, args: List[object]) -> object:
        fn = self.functions.get(name)
        if fn is None:
            builtin = self.global_env.get(name)
            if callable(builtin):
                return builtin(*args)
            raise PacRuntimeError(f"Function {name!r} is not defined")
        env = Environment(parent=fn.closure)
        for i, param in enumerate(fn.params):
            env.declare(param, args[i] if i < len(args) else _UNDEFINED)
        try:
            for stmt in fn.body:
                self._exec(stmt, env)
        except _ReturnSignal as r:
            return r.value
        return _UNDEFINED

    def _exec(self, node: Node, env: Environment) -> None:
        if isinstance(node, ExprStmt):
            self._eval(node.expr, env)
        elif isinstance(node, VarDecl):
            env.declare(node.name, self._eval(node.value, env) if node.value else _UNDEFINED)
        elif isinstance(node, ReturnStmt):
            raise _ReturnSignal(self._eval(node.value, env) if node.value else _UNDEFINED)
        elif isinstance(node, IfStmt):
            if _truthy(self._eval(node.test, env)):
                for s in node.consequent:
                    self._exec(s, env)
            elif node.alternate:
                for s in node.alternate:
                    self._exec(s, env)
        elif isinstance(node, Block):
            for s in node.body:
                self._exec(s, env)
        else:
            raise PacRuntimeError(f"Cannot execute node {type(node).__name__}")

    def _eval(self, node: Node, env: Environment) -> object:
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, Identifier):
            return env.get(node.name)
        if isinstance(node, ArrayLiteral):
            return _JSArray([self._eval(e, env) for e in node.elements])
        if isinstance(node, Call):
            callee = node.callee
            method = None
            obj = None
            if isinstance(callee, Member):
                obj = self._eval(callee.obj, env)
                if callee.computed:
                    method = self._eval(callee.prop, env)
                else:
                    assert isinstance(callee.prop, Literal)
                    method = callee.prop.value
            else:
                name_node = callee
                name = name_node.name if isinstance(name_node, Identifier) else None
                if name and name in self.functions:
                    fn = self.functions[name]
                    args = [self._eval(a, env) for a in node.args]
                    return self._call_user_function(fn, args, env)
                builtin = env.get(name) if name else _UNDEFINED
                if callable(builtin):
                    args = [self._eval(a, env) for a in node.args]
                    return builtin(*args)
                raise PacRuntimeError(f"{name!r} is not a function")
            if obj is None or obj is _UNDEFINED:
                raise PacRuntimeError(f"Cannot read property {method!r} of undefined")
            args = [self._eval(a, env) for a in node.args]
            return self._call_method(obj, method, args)
        if isinstance(node, Member):
            obj = self._eval(node.obj, env)
            if obj is None or obj is _UNDEFINED:
                return _UNDEFINED
            prop = self._eval(node.prop, env) if node.computed else node.prop.value
            return self._get_property(obj, prop)
        if isinstance(node, Unary):
            arg = self._eval(node.arg, env)
            return _UNARY_OPS[node.op](arg)
        if isinstance(node, Binary):
            left = self._eval(node.left, env)
            right = self._eval(node.right, env)
            return _binary_op(node.op, left, right)
        if isinstance(node, Logical):
            left = self._eval(node.left, env)
            if node.op == "&&":
                return self._eval(node.right, env) if _truthy(left) else left
            return left if _truthy(left) else self._eval(node.right, env)
        if isinstance(node, Conditional):
            return self._eval(node.consequent, env) if _truthy(self._eval(node.test, env)) else self._eval(node.alternate, env)
        if isinstance(node, Assign):
            return self._do_assign(node, env)
        raise PacRuntimeError(f"Cannot evaluate node {type(node).__name__}")

    def _call_user_function(self, fn: _JSFunction, args: List[object], caller_env: Environment) -> object:
        env = Environment(parent=fn.closure)
        for i, param in enumerate(fn.params):
            env.declare(param, args[i] if i < len(args) else _UNDEFINED)
        try:
            for stmt in fn.body:
                self._exec(stmt, env)
        except _ReturnSignal as r:
            return r.value
        return _UNDEFINED

    def _call_method(self, obj: object, method: object, args: List[object]) -> object:
        if isinstance(obj, str):
            return self._string_method(obj, str(method) if isinstance(method, str) else _to_string(method), args)
        if isinstance(obj, _JSArray):
            return self._array_method(obj, str(method) if isinstance(method, str) else _to_string(method), args)
        if isinstance(obj, float) or isinstance(obj, int):
            return self._number_method(obj, str(method) if isinstance(method, str) else _to_string(method), args)
        prop = self._get_property(obj, method)
        if callable(prop):
            return prop(*args)
        if prop is _UNDEFINED:
            raise PacRuntimeError(f"{method!r} is not a function")
        return prop

    def _get_property(self, obj: object, prop: object) -> object:
        if isinstance(prop, float) and prop == int(prop):
            prop = int(prop)
        key = _to_string(prop)
        if isinstance(obj, str):
            if key == "length":
                return len(obj)
            if key == "charAt":
                return lambda i=0: obj[int(_to_number(i))] if 0 <= int(_to_number(i)) < len(obj) else ""
            if key == "indexOf":
                return lambda s="", f=0: obj.find(_to_string(s), int(_to_number(f)))
            if key == "substring":
                return lambda a=0, b=None: obj[int(_to_number(a)): int(_to_number(b))] if b is not None else obj[int(_to_number(a)):]
            if key == "substr":
                return lambda a=0, b=None: obj[int(_to_number(a)): int(_to_number(a)) + int(_to_number(b))] if b is not None else obj[int(_to_number(a)):]
            if key == "toLowerCase":
                return lambda: obj.lower()
            if key == "toUpperCase":
                return lambda: obj.upper()
            if key == "split":
                return lambda sep=",": _JSArray(obj.split(_to_string(sep)))
            if key == "replace":
                return lambda a, b: obj.replace(_to_string(a), _to_string(b))
            if key == "slice":
                return lambda a=0, b=None: obj[int(_to_number(a)): int(_to_number(b))] if b is not None else obj[int(_to_number(a)):]
            if key == "toString":
                return lambda: obj
            return _UNDEFINED
        if isinstance(obj, _JSArray):
            if key == "length":
                return len(obj._items)
            if key == "join":
                return lambda sep=",": sep.join(_to_string(x) if x is not _UNDEFINED and x is not None else "" for x in obj._items)
            if key == "push":
                def push(*items):
                    obj._items.extend(items)
                    return len(obj._items)
                return push
            if key == "pop":
                return lambda: obj._items.pop() if obj._items else _UNDEFINED
            if key == "slice":
                return lambda a=0, b=None: _JSArray(obj._items[int(_to_number(a)): int(_to_number(b))] if b is not None else obj._items[int(_to_number(a)):])
            if key == "indexOf":
                return lambda v=0, s=0: (lambda: (lambda idx: idx if idx >= 0 else -1)())() if False else (lambda val, start=0: (lambda idx: idx if 0 <= idx else -1)(obj._items.index(val) if val in obj._items[max(0, int(_to_number(start))):] else -1))(*[v, s][:1])
            if isinstance(prop, int) and 0 <= prop < len(obj._items):
                return obj._items[prop]
            return _UNDEFINED
        if isinstance(obj, (int, float)):
            if key == "toString":
                return lambda: _to_string(obj)
            if key == "toFixed":
                return lambda d=0: f"{float(obj):.{int(_to_number(d))}f}"
            return _UNDEFINED
        if hasattr(obj, key):
            return getattr(obj, key)
        return _UNDEFINED

    def _string_method(self, s: str, method: str, args: List[object]) -> object:
        if method == "charAt":
            i = int(_to_number(args[0])) if args else 0
            return s[i] if 0 <= i < len(s) else ""
        if method == "indexOf":
            sub = _to_string(args[0]) if args else "undefined"
            start = int(_to_number(args[1])) if len(args) > 1 else 0
            return s.find(sub, start)
        if method == "lastIndexOf":
            sub = _to_string(args[0]) if args else "undefined"
            return s.rfind(sub)
        if method == "substring":
            a = int(_to_number(args[0])) if args else 0
            b = int(_to_number(args[1])) if len(args) > 1 else len(s)
            return s[min(a, b): max(a, b)]
        if method == "substr":
            start = int(_to_number(args[0])) if args else 0
            length = int(_to_number(args[1])) if len(args) > 1 else len(s)
            return s[max(0, start): max(0, start) + length]
        if method == "slice":
            a = int(_to_number(args[0])) if args else 0
            b = int(_to_number(args[1])) if len(args) > 1 else len(s)
            return s[a:b]
        if method == "toLowerCase":
            return s.lower()
        if method == "toUpperCase":
            return s.upper()
        if method == "split":
            sep = _to_string(args[0]) if args else ","
            return _JSArray(s.split(sep))
        if method == "replace":
            a = _to_string(args[0]) if args else ""
            b = _to_string(args[1]) if len(args) > 1 else ""
            return s.replace(a, b, 1)
        if method == "trim":
            return s.strip()
        if method == "concat":
            return s + "".join(_to_string(a) for a in args)
        if method == "match":
            return _UNDEFINED
        if method == "toString":
            return s
        raise PacRuntimeError(f"String method {method!r} not supported")

    def _array_method(self, arr: _JSArray, method: str, args: List[object]) -> object:
        if method == "join":
            sep = _to_string(args[0]) if args else ","
            return sep.join(_to_string(x) if x is not _UNDEFINED and x is not None else "" for x in arr._items)
        if method == "push":
            arr._items.extend(args)
            return len(arr._items)
        if method == "pop":
            return arr._items.pop() if arr._items else _UNDEFINED
        if method == "shift":
            return arr._items.pop(0) if arr._items else _UNDEFINED
        if method == "unshift":
            arr._items[:0] = list(args)
            return len(arr._items)
        if method == "slice":
            a = int(_to_number(args[0])) if args else 0
            b = int(_to_number(args[1])) if len(args) > 1 else len(arr._items)
            return _JSArray(arr._items[a:b])
        if method == "concat":
            items = list(arr._items)
            for a in args:
                if isinstance(a, _JSArray):
                    items.extend(a._items)
                else:
                    items.append(a)
            return _JSArray(items)
        if method == "indexOf":
            val = args[0] if args else _UNDEFINED
            start = int(_to_number(args[1])) if len(args) > 1 else 0
            for i in range(max(0, start), len(arr._items)):
                if _strict_equals(arr._items[i], val):
                    return i
            return -1
        if method == "reverse":
            arr._items.reverse()
            return arr
        if method == "sort":
            arr._items.sort(key=_to_string)
            return arr
        if method == "toString":
            return ",".join(_to_string(x) if x is not _UNDEFINED and x is not None else "" for x in arr._items)
        raise PacRuntimeError(f"Array method {method!r} not supported")

    def _number_method(self, n: object, method: str, args: List[object]) -> object:
        if method == "toString":
            return _to_string(n)
        if method == "toFixed":
            d = int(_to_number(args[0])) if args else 0
            return f"{float(n):.{d}f}"
        if method == "valueOf":
            return n
        raise PacRuntimeError(f"Number method {method!r} not supported")

    def _do_assign(self, node: Assign, env: Environment) -> object:
        if isinstance(node.target, Identifier):
            name = node.target.name
            val = self._eval(node.value, env)
            if node.op == "=":
                if not env.set_existing(name, val):
                    env.declare(name, val)
                return val
            cur = env.get(name)
            new = _binary_op(node.op[0], cur, val)
            if not env.set_existing(name, new):
                env.declare(name, new)
            return new
        if isinstance(node.target, Member):
            obj = self._eval(node.target.obj, env)
            prop = self._eval(node.target.prop, env) if node.target.computed else node.target.prop.value
            cur = self._get_property(obj, prop)
            val = self._eval(node.value, env)
            if node.op == "=":
                new = val
            else:
                new = _binary_op(node.op[0], cur, val)
            if isinstance(obj, _JSArray) and isinstance(prop, (int, float)):
                idx = int(prop)
                while len(obj._items) <= idx:
                    obj._items.append(_UNDEFINED)
                obj._items[idx] = new
            return new
        raise PacRuntimeError("Invalid assignment target")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class PACEvaluator:
    """Parse a PAC script and evaluate ``FindProxyForURL`` for any URL.

    ``dns_resolver`` is injectable so tests can avoid real network lookups
    and remain deterministic. Pass a callable taking a hostname and
    returning an IP string (empty string when unresolvable).
    """

    def __init__(
        self,
        script: str,
        dns_resolver: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.script = script
        self._dns_resolver = dns_resolver or _dns_resolve
        try:
            program = _Parser(script).parse()
        except PacSyntaxError:
            raise
        globals_ = _make_pac_globals(self._dns_resolver)
        self._interp = Interpreter(program, globals_)
        fn = self._interp.functions.get("FindProxyForURL")
        if fn is None:
            raise PacSyntaxError("PAC script does not define FindProxyForURL(url, host)")

    def find_proxy_for_url(self, url: str) -> FindProxyForURLResult:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        result = self._interp.call_function("FindProxyForURL", [url, host])
        return _parse_result(result)

    def call_function(self, name: str, *args: object) -> object:
        """Call any user-defined function in the PAC by name.

        Returns the raw interpreter value (string, number, bool, etc.).
        Useful for testing helper functions like ``FindProxyForURL``
        indirectly or utility helpers within the script.
        """
        return self._interp.call_function(name, list(args))


def _parse_result(value: object) -> FindProxyForURLResult:
    if value is _UNDEFINED or value is None:
        return FindProxyForURLResult(value="", direct=False, proxies=[])
    s = _to_string(value)
    parts = [p.strip() for p in s.split(";") if p.strip()]
    proxies: List[str] = []
    direct = False
    for p in parts:
        token = p.split()[0].upper() if p.split() else ""
        if token == Proxies.DIRECT:
            direct = True
            proxies.append(Proxies.DIRECT)
        elif token in (Proxies.PROXY, Proxies.SOCKS, Proxies.SOCKS4, Proxies.SOCKS5, Proxies.HTTPS):
            proxies.append(p)
        else:
            proxies.append(p)
    return FindProxyForURLResult(value=s, direct=direct, proxies=proxies)


def evaluate_pac(
    script: str,
    url: str,
    dns_resolver: Optional[Callable[[str], str]] = None,
) -> FindProxyForURLResult:
    """One-shot helper: parse ``script``, evaluate ``FindProxyForURL(url, host)``,
    return the structured result.

    Equivalent to::

        PACEvaluator(script, dns_resolver).find_proxy_for_url(url)
    """
    return PACEvaluator(script, dns_resolver).find_proxy_for_url(url)
