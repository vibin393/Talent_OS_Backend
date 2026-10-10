"""Boolean search query parser (Epic 5, FR-27). Pure: no DB, no I/O.

Grammar (operators are UPPERCASE only; lowercase ``and/or/not`` are ordinary terms)::

    expr  := or
    or    := and ("OR" and)*
    and   := unary (("AND")? unary)*          # adjacent atoms = implicit AND
    unary := "NOT" unary | "-" unary | atom
    atom  := PHRASE | TERM["*"] | "(" expr ")"

Limits: 1,000 characters, 64 atoms, nesting depth 8. Errors raise ``QuerySyntaxError(position, message)``
with a **1-based** character position (``databricks AND (azure`` -> 16, "Unclosed parenthesis").

The AST never reaches SQL as text: ``app.services.search.candidate_search`` turns every leaf into a bound
parameter (``plainto_tsquery`` / ``phraseto_tsquery`` / ``to_tsquery`` of sanitised lexemes).
"""

import dataclasses
import re
from typing import Literal

MAX_LENGTH = 1_000
MAX_ATOMS = 64
MAX_DEPTH = 8

_OPERATORS = {"AND", "OR", "NOT"}
_TERM_STOP = set('()"') | {" ", "\t", "\n", "\r"}
_LEXEME_RE = re.compile(r"[^\W_]+", re.UNICODE)


class QuerySyntaxError(ValueError):
    def __init__(self, position: int, message: str) -> None:
        self.position = position
        self.message = message
        super().__init__(f"{message} (position {position})")


# ================================================================ AST
@dataclasses.dataclass(frozen=True, slots=True)
class Term:
    text: str
    prefix: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class Phrase:
    text: str


@dataclasses.dataclass(frozen=True, slots=True)
class Not:
    child: "Node"


@dataclasses.dataclass(frozen=True, slots=True)
class And:
    children: tuple["Node", ...]


@dataclasses.dataclass(frozen=True, slots=True)
class Or:
    children: tuple["Node", ...]


Node = Term | Phrase | Not | And | Or


# ================================================================ tokens
TokenKind = Literal["LPAREN", "RPAREN", "PHRASE", "TERM", "AND", "OR", "NOT", "MINUS"]


@dataclasses.dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str
    position: int  # 1-based
    prefix: bool = False


def tokenize(query: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(query)
    while i < n:
        ch = query[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(Token("LPAREN", ch, i + 1))
            i += 1
            continue
        if ch == ")":
            tokens.append(Token("RPAREN", ch, i + 1))
            i += 1
            continue
        if ch == '"':
            end = query.find('"', i + 1)
            if end < 0:
                raise QuerySyntaxError(i + 1, "Unclosed quote")
            text = " ".join(query[i + 1 : end].split())
            if not text:
                raise QuerySyntaxError(i + 1, "Empty phrase")
            tokens.append(Token("PHRASE", text, i + 1))
            i = end + 1
            continue
        if ch == "-" and i + 1 < n and query[i + 1] not in _TERM_STOP and query[i + 1] != "-":
            tokens.append(Token("MINUS", ch, i + 1))
            i += 1
            continue
        start = i
        while i < n and query[i] not in _TERM_STOP:
            i += 1
        word = query[start:i]
        if word in _OPERATORS:
            tokens.append(Token(word, word, start + 1))  # type: ignore[arg-type]
            continue
        prefix = word.endswith("*")
        stem = word.rstrip("*")
        if not stem or not _LEXEME_RE.search(stem):
            raise QuerySyntaxError(start + 1, "Search term has no letters or digits")
        tokens.append(Token("TERM", stem, start + 1, prefix=prefix))
    return tokens


# ================================================================ parser
class _Parser:
    def __init__(self, tokens: list[Token], length: int) -> None:
        self.tokens = tokens
        self.i = 0
        self.atoms = 0
        self.end_position = length + 1

    def peek(self) -> Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def parse(self) -> Node:
        if not self.tokens:
            raise QuerySyntaxError(1, "Empty query")
        node = self.parse_or(0)
        tok = self.peek()
        if tok is not None:
            if tok.kind == "RPAREN":
                raise QuerySyntaxError(tok.position, "Unexpected closing parenthesis")
            raise QuerySyntaxError(tok.position, f"Unexpected {tok.value}")  # pragma: no cover
        return node

    def parse_or(self, depth: int) -> Node:
        parts = [self.parse_and(depth)]
        while (tok := self.peek()) is not None and tok.kind == "OR":
            self.take()
            nxt = self.peek()
            if nxt is None or nxt.kind in {"OR", "AND", "RPAREN"}:
                raise QuerySyntaxError(tok.position, "OR needs a term on both sides")
            parts.append(self.parse_and(depth))
        return parts[0] if len(parts) == 1 else Or(tuple(parts))

    def parse_and(self, depth: int) -> Node:
        first = self.peek()
        if first is not None and first.kind in {"AND", "OR"}:
            raise QuerySyntaxError(first.position, f"{first.kind} needs a term on both sides")
        parts = [self.parse_unary(depth)]
        while (tok := self.peek()) is not None and tok.kind not in {"OR", "RPAREN"}:
            if tok.kind == "AND":
                self.take()
                nxt = self.peek()
                if nxt is None or nxt.kind in {"OR", "AND", "RPAREN"}:
                    raise QuerySyntaxError(tok.position, "AND needs a term on both sides")
            parts.append(self.parse_unary(depth))
        return parts[0] if len(parts) == 1 else And(tuple(parts))

    def parse_unary(self, depth: int) -> Node:
        tok = self.peek()
        if tok is not None and tok.kind in {"NOT", "MINUS"}:
            self.take()
            nxt = self.peek()
            if nxt is None or nxt.kind in {"OR", "AND", "RPAREN"}:
                raise QuerySyntaxError(tok.position, f"{'NOT' if tok.kind == 'NOT' else '-'} needs a term")
            return Not(self.parse_unary(depth))
        return self.parse_atom(depth)

    def parse_atom(self, depth: int) -> Node:
        tok = self.peek()
        if tok is None:  # pragma: no cover - guarded by callers
            raise QuerySyntaxError(self.end_position, "Unexpected end of query")
        if tok.kind == "LPAREN":
            self.take()
            if depth + 1 > MAX_DEPTH:
                raise QuerySyntaxError(tok.position, f"Too many nested parentheses (max {MAX_DEPTH})")
            nxt = self.peek()
            if nxt is None:
                raise QuerySyntaxError(tok.position, "Unclosed parenthesis")
            if nxt.kind == "RPAREN":
                raise QuerySyntaxError(tok.position, "Empty parentheses")
            node = self.parse_or(depth + 1)
            close = self.peek()
            if close is None or close.kind != "RPAREN":
                raise QuerySyntaxError(tok.position, "Unclosed parenthesis")
            self.take()
            return node
        if tok.kind == "RPAREN":
            raise QuerySyntaxError(tok.position, "Unexpected closing parenthesis")
        if tok.kind in {"TERM", "PHRASE"}:
            self.take()
            self.atoms += 1
            if self.atoms > MAX_ATOMS:
                raise QuerySyntaxError(tok.position, f"Too many search terms (max {MAX_ATOMS})")
            return Phrase(tok.value) if tok.kind == "PHRASE" else Term(tok.value, tok.prefix)
        raise QuerySyntaxError(tok.position, f"{tok.value} needs a term on both sides")  # pragma: no cover


def is_negative(node: Node) -> bool:
    """Return True when ``node`` can match documents without any positive term (pure negation)."""
    if isinstance(node, Not):
        return True
    if isinstance(node, And):
        return all(is_negative(c) for c in node.children)
    if isinstance(node, Or):
        return any(is_negative(c) for c in node.children)
    return False


def parse(query: str) -> Node:
    """Parse a Boolean query; raises ``QuerySyntaxError``."""
    if len(query) > MAX_LENGTH:
        raise QuerySyntaxError(MAX_LENGTH + 1, f"Query is too long (max {MAX_LENGTH} characters)")
    tokens = tokenize(query)
    node = _Parser(tokens, len(query)).parse()
    if is_negative(node):
        raise QuerySyntaxError(1, "A query needs at least one term that is not negated")
    return node


def is_single_atom(node: Node) -> bool:
    return isinstance(node, Term | Phrase)


def lexemes(text: str) -> list[str]:
    """Sanitised lexemes (letters/digits only) of a term: the only text that reaches ``to_tsquery``."""
    return [m.group(0).lower() for m in _LEXEME_RE.finditer(text)]


def positive_terms(node: Node) -> list[str]:
    """Lower-cased positive term / phrase texts (for highlighting matched skills)."""
    if isinstance(node, Term | Phrase):
        return [node.text.lower()]
    if isinstance(node, Not):
        return []
    out: list[str] = []
    for child in node.children:
        out.extend(positive_terms(child))
    return out


def count_atoms(node: Node) -> int:
    if isinstance(node, Term | Phrase):
        return 1
    if isinstance(node, Not):
        return count_atoms(node.child)
    return sum(count_atoms(c) for c in node.children)


def to_text(node: Node) -> str:
    """Canonical text of an AST (re-parses to an equal AST)."""
    if isinstance(node, Term):
        return node.text + ("*" if node.prefix else "")
    if isinstance(node, Phrase):
        return '"' + node.text.replace('"', " ") + '"'
    if isinstance(node, Not):
        return "NOT " + to_text(node.child)
    joiner = " AND " if isinstance(node, And) else " OR "
    return "(" + joiner.join(to_text(c) for c in node.children) + ")"
