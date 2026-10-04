#!/usr/bin/env python3
"""Small stdlib-only JSON Schema subset checker for emitted report conformance.

RESTRICTED SCHEMA PROFILE
P1 `$id` and `$schema` are allowed ONLY at the document root. `$comment` is an annotation.
   `$anchor`, `$dynamicRef`,
   `$dynamicAnchor`, `$recursiveRef`, `$vocabulary` are forbidden everywhere.
P2 `$defs` ONLY at the root. A `$defs` anywhere else is rejected.
P3 `$ref` must match `^#/\\$defs/[A-Za-z0-9_-]+$` and name an existing root `$defs` entry. No
   percent-escapes, no `~` escapes, no other pointers. A `$ref` node may carry only
   annotation siblings (description, title, default, examples, x-*). No assertion siblings.
P4 No non-descending reference cycle: every in-place applicator (`$ref`, `allOf`, `anyOf`,
   `oneOf`, `not`, `if`, `then`, `else`, and `dependentSchemas`) applies to the same instance, so
   following only those edges from any def must never return to the same def. Reject such cycles in
   the gate and checker audit. Recursion through a child applicator remains auditable, but closure
   coinduction is licensed only when a cycle contains at least one declared `properties` member of
   an object closed with `additionalProperties:false`, or a `prefixItems` position of an array with
   a closed tail. A non-descending cycle raises SchemaSubsetError,
   never RecursionError. A guarded recursive tree must still pass.
P5 Integers: a JSON number is an integer iff it is finite and integral (1.0 is an integer); booleans
   are never numbers. Apply this in `type`, `oneOf` exact-one counting, `not`, `if`. Controls:
   `{"type":"integer"}` accepts 1.0 and rejects 1.5 and True; `{"type":"number"}`
   rejects True.
P6 Regex: `pattern` and `patternProperties` keys use a parsed ECMAScript subset: literal
   characters, escaped metacharacters, `.`, character classes `[...]`/`[^...]` with ranges, the
   shorthands `\\d \\D \\w \\W \\s \\S \\b \\B`, anchors `^ $`, groups `( )` and `(?: )`, alternation `|`,
   quantifiers `* + ? {n} {n,} {n,m}` (and their lazy `?` forms), with every explicit bound at
   most 1000. Everything else (lookaround, named
   groups, inline flags, backreferences, `\\A \\Z \\p` and other escapes) is rejected by the audit and by
   the gate. Matching must follow ECMAScript semantics, NOT Python's: parse to an AST and emit a
   Python expression from that AST before compiling.
   `\\d`=[0-9], `\\w`=[A-Za-z0-9_], `\\b` by ASCII word chars, `\\s` = ECMAScript WhiteSpace +
   LineTerminator set (enumerate it), `.` = any char except \\n \\r    ; then search
   (unanchored, per JSON Schema). Controls: a static expectation table, written by hand from the
   ECMAScript spec (no Node dependency), of (pattern, string, expected) rows covering each shorthand
   with an ASCII and a non-ASCII probe (e.g. "١" vs `^\\d+$` rejected, U+00A0 vs `^\\s$` accepted,
   " " vs `^.$` rejected), plus one rejected construct per forbidden class. Every in-scope
   schema's existing patterns must pass the audit. If one does not, rewrite it to the same
   ECMAScript meaning and list it.
   Character classes are intentionally restricted where host regex dialects disagree: `]` must be
   escaped as `\\]`; an unescaped `-` is a literal only as the first item (after optional `^`) or
   the last item, and may not be a range endpoint; every other `-` must form an ordered range whose
   endpoints are ordinary or escaped literals; a shorthand is forbidden on either side of a
   range hyphen. A literal hyphen elsewhere must be `\\-`.
P7 Keywords: `KEYWORDS` is the single classification table used by audit traversal, cycle edges,
   validator dispatch, and the closure gate. Unclassified keywords fail at every schema position.

This is deliberately not a permissive partial JSON Schema implementation. The whole document is
audited before an instance is checked; anything outside the profile raises ``SchemaSubsetError``.
"""
from __future__ import annotations

REPORT_SCHEMA = None
REPORT_SCHEMA_REASON = "schema checker; never writes a report"

import copy
from functools import lru_cache
import json
import math
import re
import subprocess
import sys


def rewrite_toml_integers_as_floats(text: str) -> tuple[str, int]:
    """Rewrite decimal integer tokens outside TOML strings/comments to integral floats."""
    out, index, changed, quote, escaped = [], 0, 0, None, False
    while index < len(text):
        if quote is not None:
            if text.startswith(quote, index):
                out.append(quote); index += len(quote); quote = None; escaped = False
                continue
            char = text[index]; out.append(char); index += 1
            if quote in {'"', '"""'}:
                if escaped: escaped = False
                elif char == "\\": escaped = True
            continue
        if text.startswith('"""', index) or text.startswith("'''", index):
            quote = text[index:index + 3]; out.append(quote); index += 3; continue
        if text[index] in {'"', "'"}:
            quote = text[index]; out.append(quote); index += 1; continue
        if text[index] == "#":
            newline = text.find("\n", index)
            if newline < 0: out.append(text[index:]); break
            out.append(text[index:newline + 1]); index = newline + 1; continue
        start = index
        if text[index] in "+-" and index + 1 < len(text) and text[index + 1].isdigit(): index += 1
        if text[index].isdigit():
            while index < len(text) and (text[index].isdigit() or text[index] == "_"): index += 1
            previous = text[start - 1] if start else ""
            following = text[index] if index < len(text) else ""
            token = text[start:index]
            if (previous not in "._0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                    and following not in ".:eE+-_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"):
                out.append(token + ".0"); changed += 1; continue
            out.append(token); continue
        out.append(text[index]); index += 1
    return "".join(out), changed


class SchemaSubsetError(ValueError):
    """The selected schema uses a keyword or shape this checker does not implement."""


KEYWORDS: dict[str, str] = {
    "title": "annotation", "description": "annotation", "default": "annotation",
    "examples": "annotation", "$comment": "annotation", "format": "annotation",
    "deprecated": "annotation", "readOnly": "annotation", "writeOnly": "annotation",
    "type": "assertion", "enum": "assertion", "const": "assertion",
    "required": "assertion", "minimum": "assertion", "maximum": "assertion",
    "exclusiveMinimum": "assertion", "exclusiveMaximum": "assertion",
    "minLength": "assertion", "maxLength": "assertion", "pattern": "assertion",
    "minItems": "assertion", "maxItems": "assertion",
    "minProperties": "assertion", "maxProperties": "assertion",
    "allOf": "in_place", "anyOf": "in_place", "oneOf": "in_place",
    "not": "in_place", "if": "in_place", "then": "in_place", "else": "in_place",
    "dependentSchemas": "in_place", "$ref": "in_place",
    "properties": "child", "patternProperties": "child",
    "additionalProperties": "child", "items": "child", "prefixItems": "child",
    "contains": "child", "propertyNames": "child",
    "$id": "root_only", "$schema": "root_only", "$defs": "root_only",
}
_KINDS = ("annotation", "assertion", "in_place", "child", "root_only")
_BY_KIND = {kind: frozenset(key for key, value in KEYWORDS.items() if value == kind)
            for kind in _KINDS}
assert set(KEYWORDS.values()) == set(_KINDS)
assert set().union(*_BY_KIND.values()) == set(KEYWORDS)
assert sum(len(values) for values in _BY_KIND.values()) == len(KEYWORDS)
_FORBIDDEN = {"$anchor", "$dynamicRef", "$dynamicAnchor", "$recursiveRef", "$vocabulary"}
_REF_RE = re.compile(r"^#/\$defs/[A-Za-z0-9_-]+$")

# ECMA-262 WhiteSpace plus LineTerminator code points. Keep this enumeration literal and visible:
# TAB, VT, FF, SP, NBSP, BOM, LF, CR, LS, PS, and Unicode category Zs members through U+3000.
_ECMA_SPACE_POINTS = (
    0x0009, 0x000B, 0x000C, 0x0020, 0x00A0, 0xFEFF, 0x000A, 0x000D, 0x2028, 0x2029,
    0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008,
    0x2009, 0x200A, 0x202F, 0x205F, 0x3000,
)
_ECMA_SPACE_CLASS = "".join(f"\\u{point:04x}" for point in _ECMA_SPACE_POINTS)
_ESCAPED_METACHARS = frozenset(r"^$\\.*+?()[]{}|/-")
MAX_REGEX_QUANTIFIER_BOUND = 1000


class _RegexParser:
    """Recursive-descent parser for the profile's ECMAScript-pattern grammar."""

    def __init__(self, expression: str):
        if not isinstance(expression, str):
            raise SchemaSubsetError("regex pattern must be a string")
        self.expression = expression
        self.index = 0

    def parse(self):
        tree = self._alternation()
        if self.index != len(self.expression):
            raise SchemaSubsetError(f"unexpected regex token {self.expression[self.index]!r}")
        return tree

    def _alternation(self):
        branches = [self._sequence()]
        while self._take("|"):
            branches.append(self._sequence())
        return ("alt", tuple(branches))

    def _sequence(self):
        atoms = []
        while self.index < len(self.expression) and self.expression[self.index] not in ")|":
            atoms.append(self._quantified())
        return ("seq", tuple(atoms))

    def _quantified(self):
        atom = self._atom()
        quantifier = None
        if self.index < len(self.expression) and self.expression[self.index] in "*+?":
            quantifier = self.expression[self.index]
            self.index += 1
        elif self.index < len(self.expression) and self.expression[self.index] == "{":
            start = self.index
            self.index += 1
            lower = self._digits()
            if not lower:
                raise SchemaSubsetError("only {n}, {n,}, and {n,m} quantifiers are allowed")
            upper = None
            if self._take(","):
                upper = self._digits()
                lower_value = self._quantifier_bound(lower)
                upper_value = self._quantifier_bound(upper) if upper else None
                quantifier = "{" + str(lower_value) + "," + (
                    str(upper_value) if upper_value is not None else "") + "}"
            else:
                lower_value = self._quantifier_bound(lower)
                quantifier = "{" + str(lower_value) + "}"
            if not self._take("}"):
                self.index = start
                raise SchemaSubsetError("only {n}, {n,}, and {n,m} quantifiers are allowed")
            if upper and lower_value > upper_value:
                raise SchemaSubsetError("quantifier lower bound exceeds upper bound")
        lazy = False
        if quantifier is not None and self._take("?"):
            lazy = True
        if quantifier is not None and self.index < len(self.expression) \
                and self.expression[self.index] in "*+?{":
            raise SchemaSubsetError("nested and possessive quantifiers are forbidden")
        if quantifier is not None and atom[0] in ("anchor", "assertion"):
            raise SchemaSubsetError("assertions may not be quantified")
        return ("quant", atom, quantifier, lazy) if quantifier is not None else atom

    def _atom(self):
        if self.index >= len(self.expression):
            raise SchemaSubsetError("quantifier has no atom")
        char = self.expression[self.index]
        if char in "*+?{}]":
            raise SchemaSubsetError("quantifier has no atom" if char in "*+?{" else
                                    f"unescaped {char} is outside the regex profile")
        self.index += 1
        if char == "(":
            capturing = True
            if self.expression.startswith("?:", self.index):
                capturing = False
                self.index += 2
            elif self.index < len(self.expression) and self.expression[self.index] == "?":
                raise SchemaSubsetError("lookaround, named groups, and inline flags are forbidden")
            child = self._alternation()
            if not self._take(")"):
                raise SchemaSubsetError("unbalanced regex group")
            return ("group", capturing, child)
        if char == "[":
            return self._class()
        if char == "\\":
            return self._escape(False)
        if char == ".":
            return ("dot",)
        if char in "^$":
            return ("anchor", char)
        return ("literal", char)

    def _class(self):
        negated = self._take("^")
        raw = []
        while self.index < len(self.expression):
            char = self.expression[self.index]
            if char == "]":
                self.index += 1
                break
            self.index += 1
            if char == "\\":
                raw.append(self._escape(True))
            elif char == "-":
                raw.append(("dash",))
            else:
                raw.append(("literal", char))
        else:
            raise SchemaSubsetError("unterminated character class")
        if not raw:
            raise SchemaSubsetError("empty character class is outside the regex profile")
        items = []
        cursor = 0
        while cursor < len(raw):
            item = raw[cursor]
            if item[0] == "dash":
                if cursor not in (0, len(raw) - 1):
                    raise SchemaSubsetError(
                        "unescaped hyphen must form a range or be first/last in a character class"
                    )
                # A boundary hyphen next to an internal hyphen would become that range's endpoint
                # in ECMAScript (`[--a]`, `[a--]`). Reject instead of assigning host semantics.
                neighbour = 1 if cursor == 0 else cursor - 1
                if len(raw) > 1 and raw[neighbour][0] == "dash":
                    raise SchemaSubsetError("unescaped hyphen may not be a range endpoint")
                items.append(("literal", "-"))
                cursor += 1
            elif (cursor + 2 < len(raw) and raw[cursor + 1][0] == "dash"):
                if raw[cursor][0] != "literal" or raw[cursor + 2][0] != "literal":
                    raise SchemaSubsetError("character-class range endpoints must be literals")
                if ord(raw[cursor][1]) > ord(raw[cursor + 2][1]):
                    raise SchemaSubsetError("character-class range is backwards")
                items.append(("range", raw[cursor][1], raw[cursor + 2][1]))
                cursor += 3
            else:
                items.append(item)
                cursor += 1
        return ("class", negated, tuple(items))

    def _escape(self, in_class: bool):
        if self.index >= len(self.expression):
            raise SchemaSubsetError("unterminated regex escape")
        escaped = self.expression[self.index]
        self.index += 1
        if escaped in "dDwWsS":
            return ("shorthand", escaped)
        if not in_class and escaped in "bB":
            return ("assertion", escaped)
        if escaped in _ESCAPED_METACHARS:
            return ("literal", escaped)
        raise SchemaSubsetError(f"unsupported regex escape \\{escaped}")

    def _digits(self):
        start = self.index
        while self.index < len(self.expression) and self.expression[self.index].isdigit() \
                and self.expression[self.index].isascii():
            self.index += 1
        return self.expression[start:self.index]

    @staticmethod
    def _quantifier_bound(digits: str) -> int:
        """Parse a bounded decimal without exposing the host regex/int limits."""
        normalized = digits.lstrip("0") or "0"
        maximum = str(MAX_REGEX_QUANTIFIER_BOUND)
        if len(normalized) > len(maximum) or \
                (len(normalized) == len(maximum) and normalized > maximum):
            raise SchemaSubsetError(
                f"regex quantifier bound exceeds {MAX_REGEX_QUANTIFIER_BOUND}"
            )
        return int(normalized)

    def _take(self, token: str) -> bool:
        if self.expression.startswith(token, self.index):
            self.index += len(token)
            return True
        return False


_SHORTHAND_CLASS = {"d": "0-9", "w": "A-Za-z0-9_", "s": _ECMA_SPACE_CLASS}


def _class_literal(char: str) -> str:
    if char in "^-]\\[":
        return "\\" + char
    return char


def _emit_class(node) -> str:
    _, negated, items = node
    normal = []
    complements = []
    for item in items:
        if item[0] == "literal":
            normal.append(_class_literal(item[1]))
        elif item[0] == "range":
            normal.append(_class_literal(item[1]) + "-" + _class_literal(item[2]))
        else:
            code = item[1]
            if code in "dws":
                normal.append(_SHORTHAND_CLASS[code])
            else:
                complements.append(_SHORTHAND_CLASS[code.lower()])
    if not negated:
        alternatives = (["[" + "".join(normal) + "]"] if normal else [])
        alternatives.extend("[^" + excluded + "]" for excluded in complements)
        return alternatives[0] if len(alternatives) == 1 else "(?:" + "|".join(alternatives) + ")"
    requirements = [f"(?=[{included}])" for included in complements]
    tail = "[^" + "".join(normal) + "]" if normal else "[\\s\\S]"
    return "(?:" + "".join(requirements) + tail + ")"


def _emit_pattern(node) -> str:
    kind = node[0]
    if kind == "alt":
        branches = [_emit_pattern(branch) for branch in node[1]]
        return branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
    if kind == "seq":
        return "".join(_emit_pattern(child) for child in node[1])
    if kind == "group":
        return ("(" if node[1] else "(?:") + _emit_pattern(node[2]) + ")"
    if kind == "quant":
        return _emit_pattern(node[1]) + node[2] + ("?" if node[3] else "")
    if kind == "class":
        return _emit_class(node)
    if kind == "shorthand":
        return {"d": "[0-9]", "D": "[^0-9]", "w": "[A-Za-z0-9_]",
                "W": "[^A-Za-z0-9_]", "s": "[" + _ECMA_SPACE_CLASS + "]",
                "S": "[^" + _ECMA_SPACE_CLASS + "]"}[node[1]]
    if kind == "assertion":
        word = "[A-Za-z0-9_]"
        boundary = f"(?:(?<={word})(?!{word})|(?<!{word})(?={word}))"
        non_boundary = f"(?:(?<={word})(?={word})|(?<!{word})(?!{word}))"
        return {"b": boundary, "B": non_boundary}[node[1]]
    if kind == "dot":
        return "[^\\n\\r\\u2028\\u2029]"
    if kind == "anchor":
        return "^" if node[1] == "^" else r"\Z"
    if kind == "literal":
        return re.escape(node[1])
    raise AssertionError(f"unknown regex AST node {kind!r}")


def _translate_pattern(expression: str) -> str:
    """Compatibility name: parse to an AST, then emit; never rewrite source text."""
    return _emit_pattern(_RegexParser(expression).parse())


@lru_cache(maxsize=None)
def _compile_pattern(expression: str):
    try:
        return re.compile(_translate_pattern(expression), re.ASCII)
    except re.error as exc:
        raise SchemaSubsetError(f"invalid profile regex {expression!r}: {exc}") from exc


_KNOWN_TYPES = frozenset({"null", "boolean", "integer", "number", "string", "array", "object"})


def is_json_integer(value) -> bool:
    """JSON Schema integer semantics: finite integral floats count; booleans never do."""
    return ((isinstance(value, int) and not isinstance(value, bool)) or
            (isinstance(value, float) and math.isfinite(value) and value.is_integer()))


def is_nonnegative_integer(value) -> bool:
    return is_json_integer(value) and value >= 0


def _is_json_value(value) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item)
                   for key, item in value.items())
    return False


def _audit_type_value(value) -> None:
    values = value if isinstance(value, list) else [value]
    if not values or not all(isinstance(item, str) for item in values):
        raise SchemaSubsetError("type must be a known type name or a non-empty array of them")
    if any(item not in _KNOWN_TYPES for item in values):
        raise SchemaSubsetError("type contains an unknown type name")
    if len(values) != len(set(values)):
        raise SchemaSubsetError("type array entries must be unique")


def _audit_enum_value(value) -> None:
    if not isinstance(value, list) or not value:
        raise SchemaSubsetError("enum must be a non-empty array")
    if not all(_is_json_value(item) for item in value):
        raise SchemaSubsetError("enum entries must be JSON values")


def _audit_const_value(value) -> None:
    if not _is_json_value(value):
        raise SchemaSubsetError("const must be a JSON value")


def _audit_required_value(value) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SchemaSubsetError("required must be an array of strings")
    if len(value) != len(set(value)):
        raise SchemaSubsetError("required entries must be unique")


def _audit_size_value(value) -> None:
    if not is_nonnegative_integer(value):
        raise SchemaSubsetError("size limit must be a non-negative integer")


def _audit_number_value(value) -> None:
    if not ((isinstance(value, int) and not isinstance(value, bool)) or
            (isinstance(value, float) and math.isfinite(value))):
        raise SchemaSubsetError("numeric bound must be a finite number")


def _audit_pattern_value(value) -> None:
    if not isinstance(value, str):
        raise SchemaSubsetError("pattern must be a string")
    _compile_pattern(value)


_ASSERTION_VALUE_VALIDATORS = {
    "type": _audit_type_value,
    "enum": _audit_enum_value,
    "const": _audit_const_value,
    "required": _audit_required_value,
    "minimum": _audit_number_value,
    "maximum": _audit_number_value,
    "exclusiveMinimum": _audit_number_value,
    "exclusiveMaximum": _audit_number_value,
    "minLength": _audit_size_value,
    "maxLength": _audit_size_value,
    "minItems": _audit_size_value,
    "maxItems": _audit_size_value,
    "minProperties": _audit_size_value,
    "maxProperties": _audit_size_value,
    "pattern": _audit_pattern_value,
}
assert set(_ASSERTION_VALUE_VALIDATORS) == {
    keyword for keyword, kind in KEYWORDS.items() if kind == "assertion"
}


def _audit_schema_container_value(keyword: str, value) -> None:
    """Validate applicator containers before traversal, independently of any instance."""
    if keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if not isinstance(value, list) or not value:
            raise SchemaSubsetError(f"{keyword} must be a non-empty array of schemas")
        if not all(isinstance(item, (dict, bool)) for item in value):
            raise SchemaSubsetError(f"{keyword} entries must be schemas")
    elif keyword in ("properties", "patternProperties", "dependentSchemas", "$defs"):
        if not isinstance(value, dict):
            raise SchemaSubsetError(f"{keyword} must be an object of schemas")
        if not all(isinstance(name, str) and isinstance(item, (dict, bool))
                   for name, item in value.items()):
            raise SchemaSubsetError(f"{keyword} must contain named schemas")
        if keyword == "patternProperties":
            for expression in value:
                _audit_pattern_value(expression)
    elif keyword in ("additionalProperties", "items", "contains", "propertyNames",
                     "not", "if", "then", "else"):
        if not isinstance(value, (dict, bool)):
            raise SchemaSubsetError(f"{keyword} must be a schema or boolean")


def iter_applicator_schemas(node: dict, kinds=("in_place", "child")):
    """Yield (keyword, path suffix, child schema) using only KEYWORDS for applicability."""
    for keyword, kind in KEYWORDS.items():
        if kind not in kinds or keyword not in node or keyword == "$ref":
            continue
        value = node[keyword]
        if keyword in ("properties", "patternProperties", "dependentSchemas"):
            if not isinstance(value, dict):
                raise SchemaSubsetError(f"{keyword} must be an object")
            for name, child in value.items():
                yield keyword, f".{keyword}.{name}", child
        elif keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            if not isinstance(value, list):
                raise SchemaSubsetError(f"{keyword} must be an array")
            for index, child in enumerate(value):
                yield keyword, f".{keyword}[{index}]", child
        else:
            yield keyword, f".{keyword}", value


def _pointer(document: dict, ref: str):
    if not isinstance(ref, str) or _REF_RE.fullmatch(ref) is None:
        raise SchemaSubsetError(f"reference is outside restricted root-$defs form: {ref!r}")
    definitions = document.get("$defs")
    name = ref[len("#/$defs/"):]
    if not isinstance(definitions, dict) or name not in definitions:
        raise SchemaSubsetError(f"unresolved root definition reference {ref!r}")
    value = definitions[name]
    if not isinstance(value, (dict, bool)):
        raise SchemaSubsetError(f"reference {ref!r} does not select a schema")
    return value


def _audit(node, document: dict, *, path: str, at_root: bool) -> None:
    if isinstance(node, bool):
        return
    if not isinstance(node, dict):
        raise SchemaSubsetError(f"{path}: schema node must be an object or boolean")
    for keyword in node:
        if keyword in _FORBIDDEN:
            raise SchemaSubsetError(f"{path}: forbidden schema keyword {keyword!r}")
        kind = KEYWORDS.get(keyword)
        if kind is None and not keyword.startswith("x-"):
            raise SchemaSubsetError(f"{path}: unimplemented schema keyword {keyword!r}")
        if kind == "root_only" and not at_root:
            raise SchemaSubsetError(f"{path}: {keyword} is allowed only at the document root")
        try:
            if kind == "assertion":
                _ASSERTION_VALUE_VALIDATORS[keyword](node[keyword])
            elif kind in ("in_place", "child") or keyword == "$defs":
                _audit_schema_container_value(keyword, node[keyword])
        except SchemaSubsetError as exc:
            raise SchemaSubsetError(f"{path}.{keyword}: {exc}") from exc
    if "$ref" in node:
        _pointer(document, node["$ref"])
        bad_siblings = [key for key in node if key != "$ref" and
                        KEYWORDS.get(key) != "annotation" and not key.startswith("x-")]
        if bad_siblings:
            raise SchemaSubsetError(f"{path}: $ref has assertion/non-profile sibling(s) {bad_siblings!r}")
    definitions = node.get("$defs", {})
    if at_root:
        if not isinstance(definitions, dict):
            raise SchemaSubsetError(f"{path}.$defs must be an object")
        for name, child in definitions.items():
            _audit(child, document, path=f"{path}.$defs.{name}", at_root=False)
    try:
        children = iter_applicator_schemas(node)
        for _keyword, suffix, child in children:
            _audit(child, document, path=path + suffix, at_root=False)
    except SchemaSubsetError as exc:
        if str(exc).startswith(path):
            raise
        raise SchemaSubsetError(f"{path}.{exc}") from exc


def _in_place_targets(node, document: dict) -> set[str]:
    """Return defs reachable without crossing an instance-child position."""
    if isinstance(node, bool):
        return set()
    targets = set()
    if "$ref" in node:
        _pointer(document, node["$ref"])
        targets.add(node["$ref"][len("#/$defs/"):])
    for _keyword, _suffix, child in iter_applicator_schemas(node, kinds=("in_place",)):
        targets.update(_in_place_targets(child, document))
    return targets


def _audit_non_descending_cycles(document: dict) -> None:
    definitions = document.get("$defs", {})
    graph = {name: _in_place_targets(child, document) for name, child in definitions.items()}

    def visit(origin: str, current: str, trail: tuple[str, ...]) -> None:
        for target in graph.get(current, set()):
            if target == origin:
                chain = " -> ".join((*trail, current, target))
                raise SchemaSubsetError(f"$.$defs.{origin}: non-descending reference cycle ({chain})")
            if target not in trail and target != current:
                visit(origin, target, (*trail, current))

    for name in graph:
        visit(name, name, ())


def _audit_document(document: dict) -> None:
    if not isinstance(document, dict):
        raise SchemaSubsetError("document root must be a schema object")
    _audit(document, document, path="$", at_root=True)
    _audit_non_descending_cycles(document)


def audit(document: dict) -> None:
    """Public whole-document profile audit used by both checker callers and the closure gate."""
    _audit_document(document)


def json_equal(a, b) -> bool:
    """JSON equality: booleans are not numbers; numeric int/float values compare numerically."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        return (isinstance(a, (int, float)) and not isinstance(a, bool) and
                isinstance(b, (int, float)) and not isinstance(b, bool) and a == b)
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if isinstance(a, list) or isinstance(b, list):
        return (isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and
                all(json_equal(left, right) for left, right in zip(a, b)))
    if isinstance(a, dict) or isinstance(b, dict):
        return (isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys() and
                all(json_equal(a[key], b[key]) for key in a))
    return False


def _is_type(value, expected: str) -> bool:
    integer_object = isinstance(value, int) and not isinstance(value, bool)
    float_object = isinstance(value, float)
    finite_float = float_object and math.isfinite(value)
    number = integer_object or finite_float
    integer = is_json_integer(value)
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": integer,
        "number": number,
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(expected, False)


_VALIDATOR_DISPATCH = {}


def _implements(*keywords):
    def decorate(function):
        for keyword in keywords:
            assert KEYWORDS.get(keyword) in ("assertion", "in_place", "child")
            assert keyword not in _VALIDATOR_DISPATCH
            _VALIDATOR_DISPATCH[keyword] = function
        return function
    return decorate


@_implements("type")
def _validate_type(value, node, document, path, errors, keyword):
    types = node[keyword] if isinstance(node[keyword], list) else [node[keyword]]
    if not types or not all(isinstance(item, str) for item in types):
        raise SchemaSubsetError("type must be a string or non-empty array of strings")
    unknown = [item for item in types if item not in _KNOWN_TYPES]
    if unknown:
        raise SchemaSubsetError(f"unsupported JSON Schema type(s) {unknown!r}")
    if not any(_is_type(value, item) for item in types):
        errors.append(f"{path}: expected type {node[keyword]!r}, got {type(value).__name__}")


@_implements("enum")
def _validate_enum(value, node, document, path, errors, keyword):
    if not isinstance(node[keyword], list):
        raise SchemaSubsetError("enum must be an array")
    if not any(json_equal(value, candidate) for candidate in node[keyword]):
        errors.append(f"{path}: value {value!r} is not in enum {node[keyword]!r}")


@_implements("const")
def _validate_const(value, node, document, path, errors, keyword):
    if not json_equal(value, node[keyword]):
        errors.append(f"{path}: value {value!r} does not equal const {node[keyword]!r}")


@_implements("allOf", "anyOf", "oneOf")
def _validate_combinator(value, node, document, path, errors, keyword):
    attempts = [_check(value, branch, document, path) for branch in node[keyword]]
    matches = sum(not attempt for attempt in attempts)
    if keyword == "allOf":
        errors.extend(error for attempt in attempts for error in attempt)
    elif keyword == "anyOf" and matches == 0:
        errors.append(f"{path}: value matches no anyOf branch")
    elif keyword == "oneOf" and matches != 1:
        errors.append(f"{path}: value matches {matches} oneOf branches, expected exactly one")


@_implements("not")
def _validate_not(value, node, document, path, errors, keyword):
    if not _check(value, node[keyword], document, path):
        errors.append(f"{path}: value matches forbidden not schema")


@_implements("if")
def _validate_if(value, node, document, path, errors, keyword):
    branch = "then" if not _check(value, node[keyword], document, path) else "else"
    if branch in node:
        errors.extend(_check(value, node[branch], document, path))


@_implements("then", "else")
def _validate_conditional_branch(value, node, document, path, errors, keyword):
    # These applicators have an effect only through the `if` handler.
    return None


@_implements("$ref")
def _validate_ref(value, node, document, path, errors, keyword):
    errors.extend(_check(value, _pointer(document, node[keyword]), document, path))


@_implements("required")
def _validate_required(value, node, document, path, errors, keyword):
    required = node[keyword]
    if not isinstance(required, list) or not all(isinstance(key, str) for key in required):
        raise SchemaSubsetError("required must be an array of strings")
    if isinstance(value, dict):
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")


@_implements("properties")
def _validate_properties(value, node, document, path, errors, keyword):
    if isinstance(value, dict):
        for key in sorted(set(value) & set(node[keyword])):
            errors.extend(_check(value[key], node[keyword][key], document, f"{path}.{key}"))


@_implements("patternProperties")
def _validate_pattern_properties(value, node, document, path, errors, keyword):
    if isinstance(value, dict):
        for expression, child in node[keyword].items():
            matcher = _compile_pattern(expression)
            for key in value:
                if matcher.search(key):
                    errors.extend(_check(value[key], child, document, f"{path}.{key}"))


def _extra_object_keys(value, node):
    matched = set()
    for expression in node.get("patternProperties", {}):
        matcher = _compile_pattern(expression)
        matched.update(key for key in value if matcher.search(key))
    return sorted(set(value) - set(node.get("properties", {})) - matched)


@_implements("additionalProperties")
def _validate_additional_properties(value, node, document, path, errors, keyword):
    if not isinstance(value, dict):
        return
    additional = node[keyword]
    extras = _extra_object_keys(value, node)
    if additional is False:
        errors.extend(f"{path}: undeclared key {key!r}" for key in extras)
    elif additional is not True:
        for key in extras:
            errors.extend(_check(value[key], additional, document, f"{path}.{key}"))


@_implements("dependentSchemas")
def _validate_dependent_schemas(value, node, document, path, errors, keyword):
    if isinstance(value, dict):
        for key, child in node[keyword].items():
            if key in value:
                errors.extend(_check(value, child, document, path))


@_implements("propertyNames")
def _validate_property_names(value, node, document, path, errors, keyword):
    if isinstance(value, dict):
        for key in value:
            errors.extend(_check(key, node[keyword], document, f"{path} key {key!r}"))


@_implements("prefixItems")
def _validate_prefix_items(value, node, document, path, errors, keyword):
    if isinstance(value, list):
        for index, child in enumerate(node[keyword][:len(value)]):
            errors.extend(_check(value[index], child, document, f"{path}[{index}]"))


@_implements("items")
def _validate_items(value, node, document, path, errors, keyword):
    if isinstance(value, list):
        start = len(node.get("prefixItems", []))
        for index in range(start, len(value)):
            errors.extend(_check(value[index], node[keyword], document, f"{path}[{index}]"))


@_implements("contains")
def _validate_contains(value, node, document, path, errors, keyword):
    if isinstance(value, list) and not any(
            not _check(item, node[keyword], document, f"{path}[{index}]")
            for index, item in enumerate(value)):
        errors.append(f"{path}: no item matches contains schema")


@_implements("minProperties", "maxProperties", "minItems", "maxItems",
             "minLength", "maxLength")
def _validate_size(value, node, document, path, errors, keyword):
    applies = ((keyword.endswith("Properties") and isinstance(value, dict)) or
               (keyword.endswith("Items") and isinstance(value, list)) or
               (keyword.endswith("Length") and isinstance(value, str)))
    limit = node[keyword]
    if not is_nonnegative_integer(limit):
        raise SchemaSubsetError(f"{keyword} must be a non-negative integer")
    if applies and keyword.startswith("min") and len(value) < limit:
        errors.append(f"{path}: has fewer than {limit} {keyword[3:].lower()}")
    if applies and keyword.startswith("max") and len(value) > limit:
        errors.append(f"{path}: has more than {limit} {keyword[3:].lower()}")


@_implements("pattern")
def _validate_pattern(value, node, document, path, errors, keyword):
    if isinstance(value, str) and _compile_pattern(node[keyword]).search(value) is None:
        errors.append(f"{path}: string does not match pattern {node[keyword]!r}")


@_implements("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
def _validate_number_bound(value, node, document, path, errors, keyword):
    limit = node[keyword]
    if not _is_type(limit, "number"):
        raise SchemaSubsetError(f"{keyword} must be a finite number")
    if not _is_type(value, "number"):
        return
    failed = ((keyword == "minimum" and value < limit) or
              (keyword == "maximum" and value > limit) or
              (keyword == "exclusiveMinimum" and value <= limit) or
              (keyword == "exclusiveMaximum" and value >= limit))
    if failed:
        errors.append(f"{path}: value violates {keyword} {limit}")


_EXPECTED_VALIDATORS = {keyword for keyword, kind in KEYWORDS.items()
                        if kind in ("assertion", "in_place", "child")}
assert set(_VALIDATOR_DISPATCH) == _EXPECTED_VALIDATORS


def _check(value, node, document: dict, path: str) -> list[str]:
    if node is True:
        return []
    if node is False:
        return [f"{path}: value is forbidden by schema"]
    errors = []
    if "type" in node:
        _VALIDATOR_DISPATCH["type"](value, node, document, path, errors, "type")
        if errors:
            return errors
    for keyword, kind in KEYWORDS.items():
        if keyword == "type" or keyword not in node or kind in ("annotation", "root_only"):
            continue
        _VALIDATOR_DISPATCH[keyword](value, node, document, path, errors, keyword)
    return errors


def conformance_errors(value, document: dict, schema, path: str = "report", *,
                       audited: bool = False) -> list[str]:
    """Return diagnostics after rejecting every profile violation in the whole document."""
    if not audited:
        audit(document)
    node = _pointer(document, schema) if isinstance(schema, str) else schema
    if node is not document and not audited:
        _audit(node, document, path="$selected", at_root=False)
    return _check(value, node, document, path)


# ---- schema-driven input differential support ---------------------------------

_JSON_TYPE_ORDER = ("null", "boolean", "integer", "number", "string", "array", "object")


def _schema_nodes_for_value(value, document: dict, schema, seen=None) -> list[dict]:
    """Return the schema nodes that apply to ``value`` at one instance location.

    This is intentionally built on the same private ``_check`` engine as
    ``conformance_errors``: the differential generator must not maintain a second interpretation
    of references, combinators, or conditionals.  Only matching anyOf/oneOf branches are followed;
    all allOf branches and the selected if/then/else branch are followed.
    """
    node = _pointer(document, schema) if isinstance(schema, str) else schema
    if node is True or node is False:
        return []
    seen = set() if seen is None else seen
    marker = id(node)
    if marker in seen:
        return []
    seen = seen | {marker}
    nodes = [node]
    if "$ref" in node:
        nodes.extend(_schema_nodes_for_value(value, document, node["$ref"], seen))
    for branch in node.get("allOf", []):
        nodes.extend(_schema_nodes_for_value(value, document, branch, seen))
    for keyword in ("anyOf", "oneOf"):
        for branch in node.get(keyword, []):
            if not _check(value, branch, document, "$candidate"):
                nodes.extend(_schema_nodes_for_value(value, document, branch, seen))
    if "if" in node:
        selected = "then" if not _check(value, node["if"], document, "$candidate") else "else"
        if selected in node:
            nodes.extend(_schema_nodes_for_value(value, document, node[selected], seen))
    return nodes


def _declared_json_types(value, document: dict, schema) -> list[str]:
    found = set()
    for node in _schema_nodes_for_value(value, document, schema):
        declared = node.get("type")
        if isinstance(declared, str):
            found.add(declared)
        elif isinstance(declared, list):
            found.update(item for item in declared if isinstance(item, str))
    if not found:
        def collect(node, seen=None):
            node = _pointer(document, node) if isinstance(node, str) else node
            seen = set() if seen is None else seen
            if not isinstance(node, dict) or id(node) in seen:
                return
            seen = seen | {id(node)}
            declared = node.get("type")
            if isinstance(declared, str): found.add(declared)
            elif isinstance(declared, list): found.update(declared)
            if "$ref" in node: collect(node["$ref"], seen)
            for keyword in ("allOf", "anyOf", "oneOf"):
                for branch in node.get(keyword, []): collect(branch, seen)
        collect(schema)
    return [kind for kind in _JSON_TYPE_ORDER if kind in found]


def _value_has_json_type(value, kind: str) -> bool:
    return _is_type(value, kind) and not (kind == "number" and _is_type(value, "integer"))


def _minimal_schema_value(document: dict, schema, kind: str, current=None, depth: int = 0):
    """Construct one locally schema-valid value of ``kind`` or raise ``ValueError``.

    The candidate list is deliberately small and deterministic.  Existing fixture values are
    preferred when they already have the requested type; enum/const values and required object
    members are discovered from the schema rather than restated by a format self-test.
    """
    if depth > 24:
        raise ValueError("schema synthesis depth exceeds 24")
    node = _pointer(document, schema) if isinstance(schema, str) else schema
    candidates = []
    if current is not None and _value_has_json_type(current, kind):
        if not _check(current, node, document, "$candidate"):
            return copy.deepcopy(current)
    if "const" in node and _is_type(node["const"], kind):
        candidates.append(copy.deepcopy(node["const"]))
    for item in node.get("enum", []):
        if _is_type(item, kind):
            candidates.append(copy.deepcopy(item))
    if "$ref" in node:
        try:
            candidates.append(_minimal_schema_value(document, node["$ref"], kind, current,
                                                    depth + 1))
        except ValueError:
            pass
    for keyword in ("allOf", "anyOf", "oneOf"):
        for branch in node.get(keyword, []):
            if kind not in _declared_json_types(current, document, branch):
                continue
            try:
                candidates.append(_minimal_schema_value(document, branch, kind, current,
                                                        depth + 1))
            except ValueError:
                pass
    if kind == "null":
        candidates.append(None)
    elif kind == "boolean":
        candidates.extend((False, True))
    elif kind in ("integer", "number"):
        lower = node.get("minimum", node.get("exclusiveMinimum", 0))
        if not isinstance(lower, (int, float)) or isinstance(lower, bool):
            lower = 0
        if "exclusiveMinimum" in node:
            lower += 1
        integer = int(math.ceil(lower))
        candidates.extend((integer, float(integer) + 0.5) if kind == "number" else (integer,))
    elif kind == "string":
        minimum = node.get("minLength", 0)
        candidates.extend(("" if minimum == 0 else "x" * minimum,
                           "x", "a", "1", "1d", "unknown",
                           "2026-01-01", "2026-01-01T00:00:00Z"))
    elif kind == "array":
        minimum = node.get("minItems", 0)
        item_schema = node.get("items", {})
        item_types = _declared_json_types(None, document, item_schema)
        item = None
        if minimum and item_types:
            item = _minimal_schema_value(document, item_schema, item_types[0], depth=depth + 1)
        candidates.append([copy.deepcopy(item) for _ in range(minimum)])
    elif kind == "object":
        value = {}
        properties = {}
        required = set()
        for applicable in _schema_nodes_for_value(current if isinstance(current, dict) else {},
                                                  document, node):
            properties.update(applicable.get("properties", {}))
            required.update(applicable.get("required", []))
        for key in sorted(required):
            child = properties.get(key, {})
            child_types = _declared_json_types(None, document, child)
            if not child_types:
                raise ValueError(f"cannot synthesize required property {key!r}")
            value[key] = _minimal_schema_value(document, child, child_types[0], depth=depth + 1)
        candidates.append(value)
    for candidate in candidates:
        if _is_type(candidate, kind) and not _check(candidate, node, document, "$candidate"):
            return candidate
    raise ValueError(f"cannot synthesize a schema-valid {kind} value")


def schema_input_variants(value, document: dict, schema) -> list[tuple[str, object]]:
    """Generate the property variants required by the input-schema/CLI differential.

    From one positive fixture this emits, at every represented object location: every optional
    property addition (one minimal valid value per declared JSON type, including null and the empty
    string when allowed), removal of every present non-required property, and assignment of every
    present property to every JSON type its schema permits.  Duplicate documents are intentional:
    a fixture's existing value is the witness for its current type and is still run through the
    real CLI under that property's coverage label.
    """
    audit(document)
    variants = []

    def changed(path, replacement):
        clone = copy.deepcopy(value)
        target = clone
        for step in path[:-1]:
            target = target[step]
        if replacement is _DELETE:
            del target[path[-1]]
        else:
            target[path[-1]] = copy.deepcopy(replacement)
        return clone

    def walk(instance, node, path=(), label="$", depth=0):
        if depth > 64:
            raise ValueError("fixture nesting exceeds differential limit 64")
        applicable = _schema_nodes_for_value(instance, document, node)
        if isinstance(instance, dict):
            properties = {}
            required = set()
            for item in applicable:
                properties.update(item.get("properties", {}))
                required.update(item.get("required", []))
            for key in sorted(properties):
                child_schema = properties[key]
                child_path = path + (key,)
                child_label = f"{label}.{key}"
                if key not in instance:
                    for kind in _declared_json_types(None, document, child_schema):
                        try:
                            candidate = _minimal_schema_value(document, child_schema, kind)
                        except ValueError as exc:
                            raise ValueError(f"{child_label}: {exc}") from exc
                        variants.append((f"add-optional:{child_label}:{kind}",
                                         changed(child_path, candidate)))
                    continue
                if key not in required:
                    variants.append((f"remove-optional:{child_label}",
                                     changed(child_path, _DELETE)))
                child = instance[key]
                for kind in _declared_json_types(child, document, child_schema):
                    candidate = _minimal_schema_value(document, child_schema, kind, child)
                    variants.append((f"allowed-type:{child_label}:{kind}",
                                     changed(child_path, candidate)))
                walk(child, child_schema, child_path, child_label, depth + 1)
        elif isinstance(instance, list):
            item_schema = next((item.get("items") for item in applicable if "items" in item), None)
            if item_schema is not None:
                for index, child in enumerate(instance):
                    walk(child, item_schema, path + (index,), f"{label}[{index}]", depth + 1)

    _DELETE = object()
    walk(value, schema)
    return variants


def toml_inline_document(value: dict) -> str:
    """Serialize the JSON-shaped TOML subset used by generated differential variants."""
    def key_text(key):
        return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)

    def scalar(item):
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int) and not isinstance(item, bool):
            return str(item)
        if isinstance(item, float) and math.isfinite(item):
            return repr(item)
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, list):
            return "[" + ", ".join(scalar(child) for child in item) + "]"
        if isinstance(item, dict):
            return "{" + ", ".join(f"{key_text(key)} = {scalar(child)}"
                                     for key, child in item.items()) + "}"
        raise ValueError(f"value {item!r} has no TOML representation")

    if not isinstance(value, dict):
        raise ValueError("TOML document must be an object")
    return "".join(f"{key_text(key)} = {scalar(item)}\n" for key, item in value.items())


_REGEX_EXPECTATIONS = (
    (r"^\d$", "7", True), (r"^\d$", "١", False),
    (r"^\D$", "A", True), (r"^\D$", "١", True), (r"^\D$", "7", False),
    (r"^\w$", "_", True), (r"^\w$", "é", False),
    (r"^\W$", "!", True), (r"^\W$", "é", True), (r"^\W$", "A", False),
    (r"^\s$", " ", True), (r"^\s$", " ", True), (r"^\s$", "", False),
    (r"^\S$", "A", True), (r"^\S$", "", True), (r"^\S$", " ", False),
    (r"^\bA\b$", "A", True), (r"^\bé\b$", "é", False),
    (r"^\BA\B$", "A", False), (r"^\Bé\B$", "é", True),
    (r"^\B$", "", True), (r"^\b$", "", False),
    (r"^.$", "x", True), (r"^.$", " ", False), (r"^.$", "\n", False),
    (r"^[\D^a]$", "5", False), (r"^[^\D^a]$", "5", True),
    (r"^[\d-]$", "-", True), (r"^[^\s]$", " ", False),
    (r"^[\W\d]$", "5", True), (r"^[\W\d]$", "a", False),
    (r"^[a-]$", "-", True), (r"^[-a]$", "-", True),
    (r"^[\-a]$", "-", True),
)
_REGEX_REJECTIONS = {
    "lookaround": r"(?=x)", "named-group": r"(?P<n>x)", "inline-flag": r"(?i:x)",
    "backreference": r"(x)\1", "python-anchor-A": r"\A", "python-anchor-Z": r"\Z",
    "unicode-property": r"\p{Letter}", "other-escape": r"\x61",
    "possessive-star": r"^a*+a$", "possessive-braces": r"a{2}+",
    "unbalanced-class": r"[a", "nested-quantifier": r"a**",
    "class-double-hyphen-left": r"^[--a]$",
    "class-double-hyphen-negated": r"^[^--a]$",
    "class-unescaped-close": r"^[]]$", "class-reversed-range": r"^[z-a]$",
    "class-shorthand-left-endpoint": r"^[\d-z]$",
    "class-shorthand-right-endpoint": r"^[a-\d]$",
    "class-shorthand-left-endpoint-negated": r"^[^\d-z]$",
    "quantifier-bound": r"a{4294967295}",
}


def _raises_subset(document: dict) -> bool:
    try:
        conformance_errors({}, document, document)
    except SchemaSubsetError:
        return True
    return False


def _module_assertion_rejects(marker: str, mutation: str) -> bool:
    """Plant a real table defect before its import-time assertion and require rejection."""
    try:
        with open(__file__, encoding="utf-8") as source_file:
            source = source_file.read()
    except OSError:
        return False
    if source.count(marker) != 1:
        return False
    planted_source = source.replace(marker, mutation + marker, 1)
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-c", planted_source],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode != 0 and "AssertionError" in proc.stderr


def direct_selftest_controls() -> dict[str, bool]:
    """Able-to-fail controls for every profile rule and the checker semantics."""
    controls = {
        "P1-nested-id": _raises_subset({"properties": {"p": {"$id": "urn:p"}}}),
        "P1-forbidden-anchor": _raises_subset({"$anchor": "x"}),
        "P2-nested-defs": _raises_subset({"allOf": [{"$defs": {"Hidden": {"type": "object"}}}]}),
        "P3-non-profile-pointer": _raises_subset({"$ref": "#/properties/q"}),
        "P3-percent-escape": _raises_subset({"$defs": {"a": {}}, "$ref": "#/$defs/%61"}),
        "P3-tilde-escape": _raises_subset({"$defs": {"a": {}}, "$ref": "#/$defs/~0"}),
        "P3-missing-def": _raises_subset({"$ref": "#/$defs/missing"}),
        "P3-assertion-sibling": _raises_subset(
            {"$defs": {"x": {"type": "string"}}, "$ref": "#/$defs/x", "type": "string"}),
        "P4-review-loop": _raises_subset({"$defs": {"loop": {"$ref": "#/$defs/loop"}},
                                         "$ref": "#/$defs/loop"}),
        "P4-indirect-combinator-loop": _raises_subset({
            "$defs": {"a": {"allOf": [{"$ref": "#/$defs/b"}]},
                      "b": {"not": {"$ref": "#/$defs/a"}}}, "type": "string"}),
        "P4-dependentSchemas-loop": _raises_subset({
            "$defs": {"loop": {"type": "object", "properties": {"p": {"type": "string"}},
                      "additionalProperties": False,
                      "dependentSchemas": {"p": {"$ref": "#/$defs/loop"}}}},
            "allOf": [{"$ref": "#/$defs/loop"}]}),
        "P4-guarded-tree-accepted": not _raises_subset({
            "$defs": {"tree": {"type": "object", "properties": {
                "children": {"type": "array", "items": {"$ref": "#/$defs/tree"}}},
                "additionalProperties": False}}, "allOf": [{"$ref": "#/$defs/tree"}]}),
        "P5-integer-accepts-1.0": not conformance_errors(1.0, {"type": "integer"}, {"type": "integer"}),
        "P5-integer-rejects-1.5": bool(conformance_errors(1.5, {"type": "integer"}, {"type": "integer"})),
        "P5-integer-rejects-true": bool(conformance_errors(True, {"type": "integer"}, {"type": "integer"})),
        "P5-number-rejects-true": bool(conformance_errors(True, {"type": "number"}, {"type": "number"})),
        "P5-review-not": bool(conformance_errors(1.0,
            {"type": "number", "not": {"type": "integer"}},
            {"type": "number", "not": {"type": "integer"}})),
        "P5-review-oneOf": bool(conformance_errors(1.0,
            {"oneOf": [{"type": "integer"}, {"type": "number"}]},
            {"oneOf": [{"type": "integer"}, {"type": "number"}]})),
        "P5-if-integral": bool(conformance_errors(1.0,
            {"if": {"type": "integer"}, "then": False, "else": True},
            {"if": {"type": "integer"}, "then": False, "else": True})),
        "P5-exclusive-minimum-accepts-half": not conformance_errors(
            0.5, {"type": "number", "exclusiveMinimum": 0},
            {"type": "number", "exclusiveMinimum": 0}),
        "P5-exclusive-minimum-rejects-zero": bool(conformance_errors(
            0, {"type": "number", "exclusiveMinimum": 0},
            {"type": "number", "exclusiveMinimum": 0})),
        "P5-size-accepts-integral-float": not conformance_errors(
            "x", {"type": "string", "minLength": 1.0},
            {"type": "string", "minLength": 1.0}),
        "P5-size-rejects-boolean": _raises_subset({"type": "string", "minLength": True}),
        "P6-large-quantifier-audit": _raises_subset(
            {"type": "string", "pattern": r"a{4294967295}"}),
        "P6-quantified-nonword-boundary": _raises_subset(
            {"type": "string", "pattern": r"^\B+$"}),
        "P6-quantified-word-boundary": _raises_subset(
            {"type": "string", "pattern": r"\b{2}"}),
        "P6-quantified-boundary-star": _raises_subset(
            {"type": "string", "pattern": r"\B*"}),
        "P7-value-type": _raises_subset({"type": ["string", "string"]}),
        "P7-value-enum": _raises_subset({"enum": []}),
        "P7-value-required": _raises_subset({"required": ["p", "p"]}),
        "P7-value-size": _raises_subset({"minLength": -1}),
        "P7-value-number": _raises_subset({"minimum": True}),
        "P7-value-pattern": _raises_subset({"pattern": 1}),
        "P7-value-schema": _raises_subset({"items": 1}),
        "P7-value-prefixItems": _raises_subset({"prefixItems": []}),
        "P7-value-schema-map": _raises_subset({"properties": []}),
        "P7-value-const": _raises_subset({"const": ("not", "json")}),
        "P7-absent-property-minLength": _raises_subset({
            "type": "object", "additionalProperties": False,
            "properties": {"p": {"type": "string", "minLength": -1}},
        }),
        "P7-unused-oneOf-minLength": _raises_subset({
            "oneOf": [{"type": "string"}, {"type": "string", "minLength": -1}],
        }),
        "P7-unknown-keyword": _raises_subset({"uniqueItems": True}),
        "P7-format-is-annotation": not _raises_subset(
            {"type": "string", "format": "date-time"}),
        "P7-comment-is-nested-annotation": not _raises_subset(
            {"properties": {"p": {"type": "string", "$comment": "annotation"}}}),
        "json-equal-bool-enum-number-rejected": bool(conformance_errors(True, {"enum": [1]}, {"enum": [1]})),
        "json-equal-int-float-accepted": not conformance_errors(1.0, {"enum": [1]}, {"enum": [1]}),
    }
    for index, (pattern, value, expected) in enumerate(_REGEX_EXPECTATIONS):
        controls[f"P6-expectation-{index:02d}"] = bool(_compile_pattern(pattern).search(value)) == expected
    for label, pattern in _REGEX_REJECTIONS.items():
        try:
            _compile_pattern(pattern)
        except SchemaSubsetError:
            controls[f"P6-reject-{label}"] = True
        else:
            controls[f"P6-reject-{label}"] = False
    probe = {"uniqueItems": True}
    positions = {"root": probe, "$defs": {"$defs": {"probe": probe}}}
    for keyword, kind in KEYWORDS.items():
        if kind not in ("in_place", "child") or keyword == "$ref":
            continue
        if keyword in ("properties", "dependentSchemas"):
            positions[keyword] = {keyword: {"p": probe}}
        elif keyword == "patternProperties":
            positions[keyword] = {keyword: {".*": probe}}
        elif keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            positions[keyword] = {keyword: [probe]}
        else:
            positions[keyword] = {keyword: probe}
    for label, planted in positions.items():
        controls[f"P7-audit-{label}"] = _raises_subset(planted)
    controls["P7-planted-validator-dispatch-gap"] = _module_assertion_rejects(
        "assert set(_VALIDATOR_DISPATCH) == _EXPECTED_VALIDATORS\n",
        "_VALIDATOR_DISPATCH.pop(next(iter(_EXPECTED_VALIDATORS)))\n",
    )
    controls["P7-planted-assertion-value-validator-gap"] = _module_assertion_rejects(
        "assert set(_ASSERTION_VALUE_VALIDATORS) == {\n",
        "_ASSERTION_VALUE_VALIDATORS.pop(next(iter(_ASSERTION_VALUE_VALIDATORS)))\n",
    )
    controls["P7-planted-kind-overlap"] = _module_assertion_rejects(
        "assert set(KEYWORDS.values()) == set(_KINDS)\n",
        "_BY_KIND['annotation'] = _BY_KIND['annotation'] | "
        "{next(iter(_BY_KIND['assertion']))}\n",
    )
    return controls


def planted_defect_controls(value: dict, document: dict, schema, *, nested_item=None) -> dict:
    """Exercise able-to-fail controls required by report self-tests."""
    controls = {}
    top = copy.deepcopy(value)
    top["__selftest_undeclared_report_key__"] = True
    controls["top-undeclared"] = bool(conformance_errors(top, document, schema))
    error_item = copy.deepcopy(value)
    error_item["errors"] = list(error_item.get("errors") or [{"code": 99, "reason": "planted control"}])
    error_item["errors"][0]["__selftest_undeclared_error_key__"] = True
    controls["errors[0]-undeclared"] = bool(conformance_errors(error_item, document, schema))
    wrong_enum = copy.deepcopy(value)
    wrong_enum["status"] = "__selftest_not_in_enum__"
    controls["wrong-enum"] = bool(conformance_errors(wrong_enum, document, schema))
    wrong_type = copy.deepcopy(value)
    wrong_type["exit_code"] = "__selftest_not_an_integer__"
    controls["wrong-type"] = bool(conformance_errors(wrong_type, document, schema))
    if nested_item is not None:
        key, index = nested_item
        nested = copy.deepcopy(value)
        if not isinstance(nested.get(key), list) or len(nested[key]) <= index:
            controls[f"{key}[{index}]-undeclared"] = False
        else:
            nested[key][index]["__selftest_undeclared_nested_key__"] = True
            controls[f"{key}[{index}]-undeclared"] = bool(conformance_errors(nested, document, schema))
    return controls


def boundary_defect_controls(script_path, builder_name: str, runner_name: str,
                             usage_argv: list[str], exception_argv: list[str],
                             run_case_builder_name: str | None = None) -> dict[str, tuple[bool, str]]:
    """Mutate real builders and require the target's real run_selftest() to return nonzero."""
    probe = r'''
import contextlib, importlib.util, io, json, os, sys
script, builder_name, runner_name, mode, argv_text, run_case_builder_name = sys.argv[1:]
sys.path.insert(0, os.path.dirname(script))
spec = importlib.util.spec_from_file_location("boundary_defect_target", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
selected_builder_name = run_case_builder_name if mode == "run-case" else builder_name
original_builder = getattr(module, selected_builder_name)
def report_errors(report):
    if not isinstance(report, dict): return []
    if isinstance(report.get("errors"), list): return report["errors"]
    validation = report.get("validation")
    if isinstance(validation, dict) and isinstance(validation.get("errors"), list):
        return validation["errors"]
    return []
def wrapped_builder(*args, **kwargs):
    report = original_builder(*args, **kwargs)
    code = report.get("exit_code") if isinstance(report, dict) else None
    if code is None and isinstance(report, dict):
        code = report.get("validation", {}).get("exit_code")
    reasons = [entry.get("reason", "") for entry in report_errors(report) if isinstance(entry, dict)]
    matches = ((mode == "usage" and code == 64) or
               (mode == "exception" and any(reason.startswith("input/runtime failure") for reason in reasons)) or
               mode == "run-case")
    if matches:
        report["__selftest_undeclared_boundary_key__"] = True
    return report
setattr(module, selected_builder_name, wrapped_builder)
module.boundary_defect_controls = lambda *args, **kwargs: {
    "usage": (True, "nested control suppressed"),
    "exception": (True, "nested control suppressed"),
    **({"run-case": (True, "nested control suppressed")} if run_case_builder_name else {}),
}
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    selftest_status = module.run_selftest()
print(json.dumps({"selftest_status": selftest_status, "tail": captured.getvalue()[-600:]}))
'''
    results = {}
    modes = [("usage", usage_argv), ("exception", exception_argv)]
    if run_case_builder_name is not None:
        modes.append(("run-case", exception_argv))
    for mode, argv in modes:
        proc = subprocess.run(
            [sys.executable, "-B", "-c", probe, str(script_path), builder_name, runner_name,
             mode, json.dumps(argv, allow_nan=False), run_case_builder_name or ""],
            capture_output=True, text=True, timeout=240,
        )
        try:
            result = json.loads(proc.stdout)
        except ValueError:
            result = {}
        status = result.get("selftest_status")
        ok = proc.returncode == 0 and isinstance(status, int) and status != 0
        detail = (f"probe_exit={proc.returncode} result={result!r} "
                  f"stderr={proc.stderr[-160:]!r}")
        results[mode] = (ok, detail)
    return results
