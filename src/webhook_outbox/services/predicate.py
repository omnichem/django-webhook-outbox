"""
Tracked table filters (``TrackedTable.predicate``, 1.2).

A filter is a small boolean expression over the fields of the tracked model.
An event is queued only for rows that match it::

    status = "paid" and total >= 100
    company.country in ("DE", "AT") and not processed = true
    archived_at is null

Grammar (keywords are case-insensitive)::

    expr        := and_expr ("or" and_expr)*
    and_expr    := unary ("and" unary)*
    unary       := "not" unary | "(" expr ")" | comparison
    comparison  := path op literal
                 | path ["not"] "in" "(" literal ("," literal)* [","] ")"
                 | path "is" ["not"] "null"
    op          := "=" | "==" | "!=" | "<" | "<=" | ">" | ">="
    path        := name ("." name)*          (at most 3 names)
    literal     := string | number | "true" | "false"
    string      := '"' ... '"' | "'" ... "'"  (escapes: \\ \" \')
    number      := ["-"] digits ["." digits]

The expression is parsed by hand and compiled into a Django ``Q`` object, so
values always reach the database as query parameters: there is no SQL, no
``eval`` and no lookup taken from user input. On top of that:

* a path may only follow forward foreign keys / one-to-one fields and end on
  a concrete field of a supported type (text, integer, float, decimal,
  boolean, UUID, or a foreign key compared by its primary key);
* ``__`` and a leading ``_`` are refused in names, so no Django lookup
  (``__regex``, ``__startswith``, ...) can be smuggled in;
* names matching ``WEBHOOK_OUTBOX["PREDICATE_DENY_FIELDS"]`` (passwords,
  tokens, secrets by default) are refused, and paths may not enter models of
  ``django.*`` apps (``user.password`` and friends);
* literals must fit the field type: ``count = "5"`` and ``name = 5`` are
  errors, ordering operators only work on numbers;
* the size is bounded: text length, nesting, comparisons and ``in`` items.

The same filter is evaluated in two ways that agree on every row:

* ``Predicate.as_q()`` for the poller (the rows are filtered in the query);
* ``Predicate.matches(instance)`` for ``enqueue()`` and signal capture.

NULL follows Django's ``exclude()``: a comparison with a missing value is
false, and its negation (``!=``, ``not in``, ``not (...)``) is true.
"""

import fnmatch
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext as _

from webhook_outbox.conf import get_setting

MAX_LENGTH = 1000
MAX_DEPTH = 10
MAX_COMPARISONS = 50
MAX_IN_ITEMS = 100
MAX_PATH_NAMES = 3
MAX_NUMBER_LENGTH = 30

KEYWORDS = frozenset({"and", "or", "not", "in", "is", "null", "true", "false"})
COMPARISON_OPS = {"=": "=", "==": "=", "!=": "!=", "<": "<", "<=": "<="}
COMPARISON_OPS.update({">": ">", ">=": ">="})
ORDERING_OPS = frozenset({"<", "<=", ">", ">="})
ORDERING_LOOKUPS = {"<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")
_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_OPERATOR = re.compile(r"==|!=|<=|>=|=|<|>")
_ESCAPES = {"\\": "\\", '"': '"', "'": "'"}


class PredicateError(ValueError):
    """The filter text is invalid. ``position`` is a 0-based offset or None."""

    def __init__(self, message: str, position: int | None = None):
        self.message = message
        self.position = position
        if position is not None:
            message = _("%(message)s (at character %(position)d)") % {
                "message": message,
                "position": position + 1,
            }
        super().__init__(message)


# --- tokens ---------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    kind: str  # name, keyword, string, number, op, "(", ")", ",", end
    value: Any
    position: int


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if char in "(),":
            tokens.append(Token(char, char, i))
            i += 1
            continue
        if char in "\"'":
            value, i_next = _read_string(text, i)
            tokens.append(Token("string", value, i))
            i = i_next
            continue
        match = _NUMBER.match(text, i)
        if match:
            raw = match.group()
            if len(raw) > MAX_NUMBER_LENGTH:
                raise PredicateError(_("The number is too long."), i)
            if match.end() < length and (
                text[match.end()].isalnum() or text[match.end()] in "_."
            ):
                raise PredicateError(_("Invalid number."), i)
            tokens.append(Token("number", raw, i))
            i = match.end()
            continue
        match = _OPERATOR.match(text, i)
        if match:
            tokens.append(Token("op", match.group(), i))
            i = match.end()
            continue
        match = _NAME.match(text, i)
        if match:
            raw = match.group()
            if match.end() < length and text[match.end()] == ".":
                raise PredicateError(_("Invalid field name."), i)
            if raw.lower() in KEYWORDS:
                tokens.append(Token("keyword", raw.lower(), i))
            else:
                tokens.append(Token("name", raw, i))
            i = match.end()
            continue
        if char == "_":
            raise PredicateError(_("Field names cannot start with '_'."), i)
        raise PredicateError(_("Unexpected character %(char)r.") % {"char": char}, i)
    tokens.append(Token("end", None, length))
    return tokens


def _read_string(text: str, start: int) -> tuple[str, int]:
    quote = text[start]
    i = start + 1
    parts = []
    while i < len(text):
        char = text[i]
        if char == "\\":
            if i + 1 >= len(text) or text[i + 1] not in _ESCAPES:
                raise PredicateError(
                    _(
                        "Invalid escape in a string: only \\\\, \\\" and \\' are allowed."
                    ),
                    i,
                )
            parts.append(_ESCAPES[text[i + 1]])
            i += 2
            continue
        if char == quote:
            return "".join(parts), i + 1
        parts.append(char)
        i += 1
    raise PredicateError(_("Unterminated string."), start)


# --- syntax tree ----------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    kind: str  # string, number, bool
    value: Any  # str for string/number (raw text), bool for bool
    position: int


@dataclass(frozen=True)
class Comparison:
    path: str
    op: str  # = != < <= > >= in "not in" "is null" "is not null"
    values: tuple[Literal, ...]
    position: int


@dataclass(frozen=True)
class BoolOp:
    op: str  # and, or
    items: tuple


@dataclass(frozen=True)
class Not:
    item: Any


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.index = 0
        self.comparisons = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def at_keyword(self, word: str) -> bool:
        return self.current.kind == "keyword" and self.current.value == word

    def expect(self, kind: str, message: str) -> Token:
        if self.current.kind != kind:
            raise PredicateError(message, self.current.position)
        return self.advance()

    def parse(self):
        node = self.parse_or(0)
        if self.current.kind != "end":
            raise PredicateError(
                _("Unexpected %(token)s; expected 'and', 'or' or the end.")
                % {"token": _describe(self.current)},
                self.current.position,
            )
        return node

    def parse_or(self, depth):
        items = [self.parse_and(depth)]
        while self.at_keyword("or"):
            self.advance()
            items.append(self.parse_and(depth))
        return items[0] if len(items) == 1 else BoolOp("or", tuple(items))

    def parse_and(self, depth):
        items = [self.parse_unary(depth)]
        while self.at_keyword("and"):
            self.advance()
            items.append(self.parse_unary(depth))
        return items[0] if len(items) == 1 else BoolOp("and", tuple(items))

    def parse_unary(self, depth):
        if depth >= MAX_DEPTH:
            raise PredicateError(
                _("The filter is nested too deeply (at most %(max)d levels).")
                % {"max": MAX_DEPTH},
                self.current.position,
            )
        if self.at_keyword("not"):
            self.advance()
            return Not(self.parse_unary(depth + 1))
        if self.current.kind == "(":
            self.advance()
            node = self.parse_or(depth + 1)
            self.expect(")", _("Expected ')'."))
            return node
        return self.parse_comparison()

    def parse_comparison(self) -> Comparison:
        token = self.current
        if token.kind != "name":
            if token.kind == "keyword":
                message = _("%(word)r is a reserved word, not a field name.") % {
                    "word": token.value
                }
            else:
                message = _("Expected a field name, got %(token)s.") % {
                    "token": _describe(token)
                }
            raise PredicateError(message, token.position)
        self.advance()
        self.comparisons += 1
        if self.comparisons > MAX_COMPARISONS:
            raise PredicateError(
                _("Too many comparisons (at most %(max)d).") % {"max": MAX_COMPARISONS},
                token.position,
            )

        if self.current.kind == "op":
            op = COMPARISON_OPS[self.advance().value]
            return Comparison(token.value, op, (self.parse_literal(),), token.position)

        if self.at_keyword("is"):
            self.advance()
            negated = False
            if self.at_keyword("not"):
                self.advance()
                negated = True
            if not self.at_keyword("null"):
                raise PredicateError(
                    _("Expected 'null' after 'is'."), self.current.position
                )
            self.advance()
            op = "is not null" if negated else "is null"
            return Comparison(token.value, op, (), token.position)

        negated = False
        if self.at_keyword("not"):
            self.advance()
            negated = True
            if not self.at_keyword("in"):
                raise PredicateError(
                    _("Expected 'in' after 'not'."), self.current.position
                )
        if self.at_keyword("in"):
            self.advance()
            self.expect("(", _("Expected '(' after 'in'."))
            values = [self.parse_literal()]
            while self.current.kind == ",":
                self.advance()
                if self.current.kind == ")":
                    break
                values.append(self.parse_literal())
                if len(values) > MAX_IN_ITEMS:
                    raise PredicateError(
                        _("Too many values in 'in' (at most %(max)d).")
                        % {"max": MAX_IN_ITEMS},
                        self.current.position,
                    )
            self.expect(")", _("Expected ',' or ')'."))
            op = "not in" if negated else "in"
            return Comparison(token.value, op, tuple(values), token.position)

        raise PredicateError(
            _("Expected an operator (=, !=, <, <=, >, >=, in, is) after %(name)r.")
            % {"name": token.value},
            self.current.position,
        )

    def parse_literal(self) -> Literal:
        token = self.current
        if token.kind in ("string", "number"):
            self.advance()
            return Literal(token.kind, token.value, token.position)
        if token.kind == "keyword" and token.value in ("true", "false"):
            self.advance()
            return Literal("bool", token.value == "true", token.position)
        if token.kind == "keyword" and token.value == "null":
            raise PredicateError(
                _("Use 'is null' or 'is not null' to compare with null."),
                token.position,
            )
        raise PredicateError(
            _("Expected a value (a string, a number, true or false), got %(token)s.")
            % {"token": _describe(token)},
            token.position,
        )


def _describe(token: Token) -> str:
    if token.kind == "end":
        return _("the end of the filter")
    return repr(token.value)


def parse(text: str):
    """Parse a filter into its syntax tree. Empty text gives None."""
    if not isinstance(text, str):
        raise PredicateError(_("The filter must be text."))
    if len(text) > MAX_LENGTH:
        raise PredicateError(
            _("The filter is too long (at most %(max)d characters).")
            % {"max": MAX_LENGTH}
        )
    if not text.strip():
        return None
    return _Parser(tokenize(text)).parse()


# --- resolution against a model -------------------------------------------------


@dataclass(frozen=True)
class _Check:
    """One comparison, resolved: Django lookup path, field chain and values."""

    lookup: str
    fields: tuple[models.Field, ...]
    value_field: models.Field
    op: str
    values: tuple


def _deny_patterns() -> list[str]:
    patterns = get_setting("PREDICATE_DENY_FIELDS") or ()
    return [p.lower() for p in patterns if isinstance(p, str)]


def _is_denied(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in patterns)


def _value_kind(field: models.Field) -> str | None:
    if isinstance(field, models.BooleanField):
        return "bool"
    if isinstance(field, models.IntegerField):
        return "int"
    if isinstance(field, models.FloatField):
        return "float"
    if isinstance(field, models.DecimalField):
        return "decimal"
    if isinstance(field, models.UUIDField):
        return "uuid"
    if isinstance(field, (models.CharField, models.TextField)):
        return "string"
    return None


def _resolve_path(model, path: str, position: int):
    names = path.split(".")
    if len(names) > MAX_PATH_NAMES:
        raise PredicateError(
            _("%(path)r is too long: at most %(max)d names.")
            % {"path": path, "max": MAX_PATH_NAMES},
            position,
        )
    patterns = _deny_patterns()
    fields = []
    current = model
    for i, name in enumerate(names):
        if "__" in name:
            raise PredicateError(
                _("%(name)r: '__' is not allowed in field names.") % {"name": name},
                position,
            )
        # Denied names are refused before the lookup, so the error does not
        # tell whether such a field exists.
        if _is_denied(name, patterns):
            raise PredicateError(
                _("Field %(name)r cannot be used in a filter.") % {"name": name},
                position,
            )
        try:
            field = current._meta.get_field(name)
        except FieldDoesNotExist:
            raise PredicateError(
                _("%(model)s has no field %(name)r.")
                % {"model": current._meta.label, "name": name},
                position,
            ) from None
        if _is_denied(field.name, patterns):
            raise PredicateError(
                _("Field %(name)r cannot be used in a filter.") % {"name": name},
                position,
            )
        if not getattr(field, "concrete", False) or field.many_to_many:
            raise PredicateError(
                _(
                    "%(name)r cannot be used in a filter: only fields of the row and "
                    "foreign keys are supported."
                )
                % {"name": name},
                position,
            )
        fields.append(field)
        last = i == len(names) - 1
        if field.is_relation and not last:
            related = field.related_model
            if related._meta.app_config.name.startswith("django."):
                raise PredicateError(
                    _(
                        "%(name)r leads to %(model)s: models of django.* apps cannot "
                        "be used in a filter."
                    )
                    % {"name": name, "model": related._meta.label},
                    position,
                )
            current = related
        elif not last:
            raise PredicateError(
                _("%(name)r is not a foreign key; nothing can follow it.")
                % {"name": name},
                position,
            )

    value_field = fields[-1]
    while value_field.is_relation:
        value_field = value_field.target_field
    return tuple(fields), value_field


def _coerce(literal: Literal, kind: str, path: str):
    def mismatch(expected):
        return PredicateError(
            _("%(path)r expects %(expected)s.") % {"path": path, "expected": expected},
            literal.position,
        )

    if kind == "string":
        if literal.kind != "string":
            raise mismatch(_("a string in quotes"))
        return literal.value
    if kind == "bool":
        if literal.kind != "bool":
            raise mismatch(_("true or false"))
        return literal.value
    if kind == "uuid":
        if literal.kind != "string":
            raise mismatch(_("a UUID in quotes"))
        try:
            return uuid.UUID(literal.value)
        except ValueError:
            raise mismatch(_("a UUID in quotes")) from None
    if literal.kind != "number":
        raise mismatch(_("a number"))
    if kind == "int":
        if "." in literal.value:
            raise mismatch(_("a whole number"))
        return int(literal.value)
    if kind == "float":
        return float(literal.value)
    return Decimal(literal.value)


def _resolve(node, model):
    if node is None:
        return None
    if isinstance(node, BoolOp):
        return BoolOp(node.op, tuple(_resolve(item, model) for item in node.items))
    if isinstance(node, Not):
        return Not(_resolve(node.item, model))

    fields, value_field = _resolve_path(model, node.path, node.position)
    kind = _value_kind(value_field)
    if kind is None:
        raise PredicateError(
            _("%(path)r is a %(type)s, which cannot be used in a filter.")
            % {"path": node.path, "type": type(value_field).__name__},
            node.position,
        )
    if node.op in ORDERING_OPS and kind not in ("int", "float", "decimal"):
        raise PredicateError(
            _("%(op)s only works on numbers; %(path)r is not a number.")
            % {"op": node.op, "path": node.path},
            node.position,
        )
    values = tuple(_coerce(literal, kind, node.path) for literal in node.values)
    lookup = "__".join(field.name for field in fields)
    return _Check(lookup, fields, value_field, node.op, values)


# --- evaluation -----------------------------------------------------------------


def _check_q(check: _Check) -> Q:
    op, lookup = check.op, check.lookup
    if op == "=":
        return Q(**{lookup: check.values[0]})
    if op == "!=":
        return ~Q(**{lookup: check.values[0]})
    if op in ORDERING_LOOKUPS:
        return Q(**{f"{lookup}__{ORDERING_LOOKUPS[op]}": check.values[0]})
    if op == "in":
        return Q(**{f"{lookup}__in": list(check.values)})
    if op == "not in":
        return ~Q(**{f"{lookup}__in": list(check.values)})
    if op == "is null":
        return Q(**{f"{lookup}__isnull": True})
    return Q(**{f"{lookup}__isnull": False})


def _to_q(node) -> Q:
    if isinstance(node, BoolOp):
        result = _to_q(node.items[0])
        for item in node.items[1:]:
            result = (
                (result & _to_q(item)) if node.op == "and" else (result | _to_q(item))
            )
        return result
    if isinstance(node, Not):
        return ~_to_q(node.item)
    return _check_q(node)


def _value_of(instance, check: _Check):
    current = instance
    for field in check.fields[:-1]:
        try:
            current = getattr(current, field.name)
        except ObjectDoesNotExist:
            return None
        if current is None:
            return None
    value = getattr(current, check.fields[-1].attname)
    if value is None:
        return None
    try:
        return check.value_field.to_python(value)
    except Exception:
        return value


def _check_matches(check: _Check, instance) -> bool:
    value = _value_of(instance, check)
    op = check.op
    if op == "is null":
        return value is None
    if op == "is not null":
        return value is not None
    if op == "=":
        return value is not None and value == check.values[0]
    if op == "!=":
        return not (value is not None and value == check.values[0])
    if op == "in":
        return value is not None and value in check.values
    if op == "not in":
        return not (value is not None and value in check.values)
    if value is None:
        return False
    try:
        if op == "<":
            return value < check.values[0]
        if op == "<=":
            return value <= check.values[0]
        if op == ">":
            return value > check.values[0]
        return value >= check.values[0]
    except TypeError:
        return False


def _matches(node, instance) -> bool:
    if isinstance(node, BoolOp):
        if node.op == "and":
            return all(_matches(item, instance) for item in node.items)
        return any(_matches(item, instance) for item in node.items)
    if isinstance(node, Not):
        return not _matches(node.item, instance)
    return _check_matches(node, instance)


class Predicate:
    """A filter compiled for one model."""

    def __init__(self, text: str, model: type[models.Model], tree):
        self.text = text
        self.model = model
        self._tree = tree

    def as_q(self) -> Q:
        """The filter as a ``Q`` object for ``model.objects.filter()``."""
        return _to_q(self._tree)

    def matches(self, instance: models.Model) -> bool:
        """Whether ``instance`` matches, read from its attributes (no query
        unless a foreign key has to be loaded)."""
        return _matches(self._tree, instance)

    def __repr__(self):
        return f"<Predicate {self.model._meta.label}: {self.text!r}>"


def compile_predicate(text: str, model: type[models.Model]) -> Predicate | None:
    """
    Parse ``text`` and resolve it against ``model``. Empty text gives None
    (no filter). Raises ``PredicateError`` when the filter is invalid.
    """
    tree = parse(text)
    if tree is None:
        return None
    return Predicate(text, model, _resolve(tree, model))
