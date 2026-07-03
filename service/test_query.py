"""Tests for query.py parser."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from query import parse_query, collect_terms, ParseError

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1; print(f"  ok  - {name}")
    else:
        failed += 1; print(f"  FAIL- {name}")

print("default OR for bare terms:")
ast = parse_query("python sqlite flask", default_op="OR")
check("top is or", ast["type"] == "or")
check("three children", len(ast["children"]) == 3)

print("default AND for bare terms:")
ast = parse_query("python sqlite", default_op="AND")
check("top is and", ast["type"] == "and")

print("explicit OR overrides default AND:")
ast = parse_query("python OR sqlite", default_op="AND")
check("top is or", ast["type"] == "or")

print("phrases:")
ast = parse_query('"machine learning"')
check("phrase node", ast["type"] == "phrase" and ast["text"] == "machine learning")

print("proximity:")
ast = parse_query("alpha %15 beta")
check("prox node", ast["type"] == "prox")
check("k=15", ast["k"] == 15)
check("two terms", len(ast["terms"]) == 2)

print("NOT / minus:")
ast = parse_query("python NOT deprecated", default_op="AND")
check("and with not child", ast["type"] == "and" and any(c["type"] == "not" for c in ast["children"]))
ast2 = parse_query("python -deprecated", default_op="AND")
check("minus parses as not", ast2["type"] == "and" and any(c["type"] == "not" for c in ast2["children"]))

print("fields:")
ast = parse_query("title:python")
check("field node", ast["type"] == "field" and ast["name"] == "title" and ast["value"] == "python")
ast = parse_query('tag:"web clipping"')
check("quoted field value", ast["type"] == "field" and ast["value"] == "web clipping")
ast = parse_query("added:>2024-01-01")
check("date field", ast["type"] == "field" and ast["name"] == "added" and ast["value"] == ">2024-01-01")

print("grouping + precedence:")
ast = parse_query("(a OR b) AND c", default_op="AND")
check("top is and", ast["type"] == "and")
check("first child is or", ast["children"][0]["type"] == "or")

print("complex:")
ast = parse_query('title:python AND ("machine learning" %20 model) NOT deprecated', default_op="AND")
check("top is and", ast["type"] == "and")
check("has 3 conjuncts", len(ast["children"]) == 3)
check("middle is prox", ast["children"][1]["type"] == "prox")

print("collect_terms excludes negated:")
terms = collect_terms(parse_query("python NOT deprecated", default_op="AND"))
check("python present", "python" in terms)
check("deprecated absent (negated)", "deprecated" not in terms)

print("errors:")
try:
    parse_query("(unclosed AND foo")
    check("unclosed paren raises", False)
except ParseError:
    check("unclosed paren raises", True)

print("empty:")
check("empty -> None", parse_query("") is None)
check("whitespace -> None", parse_query("   ") is None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
