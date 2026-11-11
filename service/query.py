"""
Advanced search query language for the clipper.

Grammar (informal):
    query      := or_expr
    or_expr    := and_expr ( ("OR" | "|") and_expr )*
    and_expr   := not_expr ( ("AND" | "&" | <implicit>) not_expr )*
                  -- bare adjacency defaults to OR at the top level (see note),
                  -- but inside an explicit AND/grouping it's AND.
    not_expr   := ("NOT" | "-") not_expr | prox_expr
    prox_expr  := atom ( "%" <int> atom )*        -- unordered proximity
    atom       := "(" or_expr ")"
                | field ":" value
                | '"' phrase '"'
                | term

Fields:
    title:, tag:, source:, url:, added:, published:, marking:, caveat:,
    class:, report: (report_number: is an alias for report:). Only these
    names lex as fields, so values containing colons (url:https://example.com)
    work unquoted.
    Date fields accept:  >YYYY-MM-DD  <YYYY-MM-DD  YYYY-MM-DD..YYYY-MM-DD  YYYY-MM-DD
    (added = date the clip was saved; published = the normalised publish date)
    marking: matches anywhere in the handling marker; class:/caveat: match the
    parsed classification level / a single caveat. All three are alias-aware,
    so caveat:nc matches NO CONTRACTORS and class:os matches OFFICIAL-SENSITIVE.

Default combination of bare terms:
    At the top level, a sequence of bare terms with no operator is treated as
    OR (matches the user's "generally OR" default). Use explicit AND / & to
    require all. Parentheses group.

The parser produces an AST of node dicts. Evaluation lives in search.py so
this module stays dependency-free and unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

# Only these names lex as field prefixes. Anything else followed by a colon
# (https:, C:, foo:bar) stays part of a plain term — otherwise a value like
# url:https://example.com would tokenize "https:" as a bogus field.
FIELD_NAMES = ("has", "missing", "title", "tag", "source", "url", "added", "published",
               "marking", "caveat", "class", "report_number", "report")

# `&` and `|` lex as operators ONLY when they stand alone as a token - i.e.
# bounded by whitespace, a paren, or a string edge. Embedded in a word they
# stay part of the term, so "AT&T", "R&D" and "url:...?a=1&b=2" tokenize as one
# term instead of being split into a spurious AND/OR. Use explicit "AND"/"OR"
# (or a space-padded "&"/"|") for the boolean operators. The negative lookbehind
# `(?<![^\s()])` means "not preceded by a term character" (start-of-string and
# after '(' both qualify); the lookahead requires the same on the right.
TOKEN_RE = re.compile(rf"""
    \s*(?:
        (?P<lparen>\() |
        (?P<rparen>\)) |
        (?P<prox>%\d+) |
        (?P<phrase>"[^"]*") |
        (?P<op>\bAND\b|\bOR\b|\bNOT\b|(?<![^\s()])[&|](?=[\s()]|$)|-) |
        (?P<field>(?:{'|'.join(FIELD_NAMES)}):) |
        (?P<term>[^\s()"%]+)
    )
""", re.VERBOSE | re.IGNORECASE)


@dataclass
class Token:
    kind: str
    value: str


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            # Skip a stray character we couldn't match.
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup
        value = m.group(kind)
        if value is None:
            continue
        # A leading '-' immediately before a term means NOT; but '-' inside a
        # term (already captured by `term`) is fine. The tokenizer emits '-'
        # as an op only when standalone.
        tokens.append(Token(kind, value))
    return tokens


# ---------------------------------------------------------------------------
# AST node constructors (plain dicts for easy JSON/debug)
# ---------------------------------------------------------------------------

def n_or(children):   return {"type": "or", "children": children}
def n_and(children):  return {"type": "and", "children": children}
def n_not(child):     return {"type": "not", "child": child}
def n_term(text):     return {"type": "term", "text": text}
def n_phrase(text):   return {"type": "phrase", "text": text}
def n_prox(terms, k): return {"type": "prox", "terms": terms, "k": k}
def n_field(name, value): return {"type": "field", "name": name.lower(), "value": value}


class ParseError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Recursive-descent parser
# ---------------------------------------------------------------------------

class Parser:
    def __init__(self, tokens: list[Token], default_op: str = "AND"):
        self.toks = tokens
        self.i = 0
        # default_op governs implicit adjacency (two atoms with no operator
        # between them). "AND" matches typical search engines; "OR" matches
        # the simple-keyword default. Explicit AND/OR/NOT always override.
        self.default_op = default_op.upper()

    def peek(self) -> Optional[Token]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self) -> Optional[Token]:
        t = self.peek()
        if t is not None:
            self.i += 1
        return t

    def parse(self):
        if not self.toks:
            return None
        node = self.parse_or()
        if self.peek() is not None:
            raise ParseError(f"unexpected trailing input near token {self.i}")
        return node

    def _is_atom_start(self, t: Token) -> bool:
        return t.kind in ("lparen", "phrase", "field", "term") or (
            t.kind == "op" and t.value.upper() == "NOT"
        ) or (t.kind == "op" and t.value == "-")

    def parse_or(self):
        children = [self.parse_and()]
        while True:
            t = self.peek()
            if t and t.kind == "op" and t.value.upper() in ("OR", "|"):
                self.next()
                children.append(self.parse_and())
            elif t and self.default_op == "OR" and self._is_atom_start(t):
                # Implicit adjacency under OR default: next atom is a sibling.
                children.append(self.parse_and())
            else:
                break
        return children[0] if len(children) == 1 else n_or(children)

    def parse_and(self):
        children = [self.parse_not()]
        while True:
            t = self.peek()
            if t is None or t.kind == "rparen":
                break
            if t.kind == "op" and t.value.upper() in ("AND", "&"):
                self.next()
                children.append(self.parse_not())
            elif t.kind == "op" and t.value.upper() in ("OR", "|"):
                break  # let parse_or handle disjunction
            elif self._is_atom_start(t):
                # Implicit adjacency. Combine per default_op. If default is OR
                # we stop here so parse_or treats the next atom as a sibling;
                # if AND we consume it into this conjunction.
                if self.default_op == "OR":
                    break
                children.append(self.parse_not())
            else:
                break
        return children[0] if len(children) == 1 else n_and(children)

    def parse_not(self):
        t = self.peek()
        if t and t.kind == "op" and t.value.upper() in ("NOT", "-"):
            self.next()
            return n_not(self.parse_not())
        return self.parse_prox()

    def parse_prox(self):
        left = self.parse_atom()
        terms = [left]
        k = None
        while True:
            t = self.peek()
            if t and t.kind == "prox":
                k = int(t.value[1:])
                self.next()
                terms.append(self.parse_atom())
            else:
                break
        if len(terms) == 1:
            return left
        return n_prox(terms, k)

    def parse_atom(self):
        t = self.next()
        if t is None:
            raise ParseError("unexpected end of query")
        if t.kind == "lparen":
            node = self.parse_or()
            closing = self.next()
            if not closing or closing.kind != "rparen":
                raise ParseError("missing closing parenthesis")
            return node
        if t.kind == "phrase":
            return n_phrase(t.value.strip('"'))
        if t.kind == "field":
            name = t.value[:-1]
            vt = self.next()
            if vt is None:
                raise ParseError(f"field {name}: missing value")
            value = vt.value.strip('"') if vt.kind == "phrase" else vt.value
            return n_field(name, value)
        if t.kind == "term":
            return n_term(t.value)
        if t.kind == "op":
            # Operator where an atom was expected: treat as literal term.
            return n_term(t.value)
        raise ParseError(f"unexpected token {t.kind}:{t.value}")


def parse_query(text: str, default_op: str = "AND"):
    """Parse a query string into an AST. Returns None for empty input.

    default_op controls how bare adjacent terms combine ("AND" or "OR").
    """
    return Parser(tokenize(text), default_op=default_op).parse()


# ---------------------------------------------------------------------------
# Collect the positive search terms from an AST (for highlighting/snippets)
# ---------------------------------------------------------------------------

def collect_terms(node) -> list[str]:
    """Return all term/phrase strings under non-negated positions."""
    out: list[str] = []

    def walk(n, negated=False):
        if n is None:
            return
        t = n["type"]
        if t == "not":
            walk(n["child"], not negated)
        elif t in ("or", "and"):
            for c in n["children"]:
                walk(c, negated)
        elif t == "prox":
            for c in n["terms"]:
                walk(c, negated)
        elif t in ("term", "phrase"):
            if not negated:
                out.append(n["text"])
        elif t == "field":
            # Only text-ish fields contribute to snippet terms.
            if not negated and n["name"] in ("title", "url", "tag", "source"):
                out.append(n["value"])

    walk(node)
    # De-dupe, keep order.
    seen = set()
    res = []
    for term in out:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            res.append(term)
    return res


if __name__ == "__main__":
    import json
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else 'title:python AND ("machine learning" %20 model) NOT deprecated added:>2024-01-01'
    print("QUERY:", q)
    print(json.dumps(parse_query(q), indent=2))
    print("TERMS:", collect_terms(parse_query(q)))
