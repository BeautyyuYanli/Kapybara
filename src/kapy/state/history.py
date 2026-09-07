"""Unicode keyword indexing and a closed, recursively scoped SQL compiler."""

import unicodedata
from decimal import Decimal
from typing import Any, LiteralString
from uuid import UUID

import sqlglot
from psycopg import sql
from sqlglot import exp

from .contracts import InvalidArgument, JsonObject, UnsafeQuery

HISTORY_KINDS = ("input", "model_request", "model_response", "final", "waiting", "error")


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _cjk(char: str) -> bool:
    return any(
        start <= ord(char) <= end
        for start, end in ((0x3400, 0x9FFF), (0x20000, 0x323AF), (0x3040, 0x30FF), (0xAC00, 0xD7AF))
    )


def words(text: str) -> list[str]:
    result: list[str] = []
    word = ""
    for char in normalized(text):
        if unicodedata.category(char)[0] in "LNM":
            word += char
        elif word:
            result.append(word)
            word = ""
    if word:
        result.append(word)
    return result


def search_tokens(text: str) -> list[str]:
    result: list[str] = []
    for word in words(text):
        result.append(word)
        for i, char in enumerate(word):
            if _cjk(char):
                result.append(char)
                if i and _cjk(word[i - 1]):
                    result.append(word[i - 1 : i + 1])
    return sorted(set(result))


def search_document(text: str) -> str:
    return " ".join(search_tokens(text))


def search_terms(text: str) -> tuple[str, tuple[str, ...]]:
    if len(text.encode()) > 16 * 1024:
        raise InvalidArgument("search query exceeds 16 KiB")
    terms: list[str] = []
    verify: list[str] = []
    for word in words(text):
        if any(_cjk(char) for char in word):
            verify.append(word)
            pairs = [
                word[i - 1 : i + 1]
                for i in range(1, len(word))
                if _cjk(word[i - 1]) and _cjk(word[i])
            ]
            terms.extend(pairs or [word])
        else:
            terms.append(word)
    # plainto_tsquery applies AND and treats tokens as data, never tsquery syntax.
    return " ".join(terms), tuple(verify)


_ALLOWED: dict[type[exp.Expression], set[str]] = {
    exp.Select: {"expressions", "from_", "joins", "where", "group", "having", "order", "limit"},
    exp.From: {"this"},
    exp.Table: {"this", "alias"},
    exp.TableAlias: {"this"},
    exp.Identifier: {"this", "quoted"},
    exp.Column: {"this", "table"},
    exp.Star: set(),
    exp.Alias: {"this", "alias"},
    exp.Subquery: {"this", "alias"},
    exp.Join: {"this", "on", "side", "kind"},
    exp.Where: {"this"},
    exp.Having: {"this"},
    exp.Group: {"expressions"},
    exp.Order: {"expressions"},
    exp.Ordered: {"this", "desc", "nulls_first"},
    exp.Limit: {"expression"},
    exp.Literal: {"this", "is_string"},
    exp.Placeholder: {"this"},
    exp.Null: set(),
    exp.Boolean: {"this"},
    exp.Paren: {"this"},
    exp.Not: {"this"},
    exp.Exists: {"this"},
    exp.In: {"this", "expressions", "query"},
    exp.Count: {"this", "big_int"},
    exp.Min: {"this"},
    exp.Max: {"this"},
    exp.Lower: {"this"},
    exp.Length: {"this"},
    exp.Coalesce: {"this", "expressions"},
}
_BINARY: dict[type[exp.Expression], LiteralString] = {
    exp.EQ: "=",
    exp.NEQ: "<>",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.And: "AND",
    exp.Or: "OR",
    exp.Is: "IS",
    exp.Like: "LIKE",
    exp.ILike: "ILIKE",
}
_ALLOWED.update({kind: {"this", "expression"} for kind in _BINARY})
_FUNCTIONS: dict[type[exp.Expression], LiteralString] = {
    exp.Count: "count",
    exp.Min: "min",
    exp.Max: "max",
    exp.Lower: "lower",
    exp.Length: "length",
}


class _Compiler:
    def __init__(self, params: JsonObject):
        self.params = params
        self.binds: dict[str, Any] = {}

    def bind(self, value: Any) -> sql.Placeholder:
        name = f"value_{len(self.binds)}"
        self.binds[name] = value
        return sql.Placeholder(name)

    def identifier(self, node: exp.Expression) -> sql.Identifier:
        if type(node) is not exp.Identifier:
            raise UnsafeQuery("identifier required")
        name = node.name if node.args.get("quoted") else node.name.lower()
        if not name or name.startswith("_kapy") or len(name.encode()) > 63:
            raise UnsafeQuery("invalid or reserved identifier")
        return sql.Identifier(name)

    def render(self, node: exp.Expression, depth: int = 0) -> sql.Composable:
        kind = type(node)

        def emit(child: exp.Expression) -> sql.Composable:
            return self.render(child, depth)

        if kind is exp.Select:
            if depth >= 4:
                raise UnsafeQuery("SQL nesting exceeds four SELECT levels")
            if not node.expressions or not node.args.get("from_"):
                raise UnsafeQuery("SELECT must read history")

            def child(n: exp.Expression) -> sql.Composable:
                return self.render(n, depth + 1)

            parts: list[sql.Composable] = [
                sql.SQL("SELECT "),
                sql.SQL(", ").join(child(n) for n in node.expressions),
            ]
            clauses: list[tuple[str, LiteralString]] = [
                ("from_", " FROM "),
                ("where", " WHERE "),
                ("group", " GROUP BY "),
                ("having", " HAVING "),
                ("order", " ORDER BY "),
                ("limit", " LIMIT "),
            ]
            for name, prefix in clauses:
                if node.args.get(name):
                    parts.extend([sql.SQL(prefix), child(node.args[name])])
                if name == "from_":
                    parts.extend(child(n) for n in node.args.get("joins", []))
            return sql.Composed(parts)
        if kind in {exp.From, exp.Where, exp.Having, exp.Limit}:
            if kind is exp.Limit:
                value = node.expression
                if not isinstance(value, exp.Literal) or not value.is_int or int(value.this) < 0:
                    raise UnsafeQuery("LIMIT must be a nonnegative integer literal")
                return emit(value)
            return emit(node.this)
        if kind is exp.Table:
            if not isinstance(node.this, exp.Identifier) or node.this.name.lower() != "history":
                raise UnsafeQuery("only the history relation is available")
            self.identifier(node.this)
            alias = node.args.get("alias")
            return sql.SQL("_kapy_history AS {}").format(
                emit(alias) if alias else sql.Identifier("history")
            )
        if kind in {exp.Identifier, exp.TableAlias}:
            return self.identifier(node if kind is exp.Identifier else node.this)
        if kind is exp.Column:
            name = emit(node.this)
            if node.args.get("table"):
                return sql.SQL("{}.{}").format(self.identifier(node.args["table"]), name)
            return name
        if kind is exp.Star:
            return sql.SQL("*")
        if kind is exp.Alias:
            return sql.SQL("{} AS {}").format(emit(node.this), self.identifier(node.args["alias"]))
        if kind is exp.Subquery:
            value = sql.SQL("({})").format(emit(node.this))
            if node.args.get("alias"):
                value = sql.SQL("{} AS {}").format(value, emit(node.args["alias"]))
            return value
        if kind is exp.Join:
            if node.args.get("side") not in {None, "", "LEFT"}:
                raise UnsafeQuery("only INNER and LEFT JOIN are allowed")
            if node.args.get("kind") not in {None, "", "INNER", "OUTER"} or not node.args.get("on"):
                raise UnsafeQuery("JOIN requires an ON expression")
            prefix = " LEFT JOIN " if node.args.get("side") == "LEFT" else " INNER JOIN "
            return sql.SQL(prefix + "{} ON {}").format(emit(node.this), emit(node.args["on"]))
        if kind in {exp.Group, exp.Order}:
            return sql.SQL(", ").join(emit(n) for n in node.expressions)
        if kind is exp.Ordered:
            suffix = " DESC" if node.args.get("desc") else " ASC"
            suffix += " NULLS FIRST" if node.args.get("nulls_first") else " NULLS LAST"
            return sql.SQL("{}" + suffix).format(emit(node.this))
        if kind is exp.Literal:
            value = (
                node.this
                if node.is_string
                else (int(node.this) if node.is_int else Decimal(node.this))
            )
            return self.bind(value)
        if kind is exp.Placeholder:
            if node.this not in self.params:
                raise InvalidArgument("missing named query parameter")
            value = self.params[node.this]
            if isinstance(value, (dict, list)):
                raise InvalidArgument("SQL parameters must be JSON scalars")
            return self.bind(value)
        if kind is exp.Null:
            return sql.SQL("NULL")
        if kind is exp.Boolean:
            return sql.SQL("TRUE" if node.this else "FALSE")
        if kind in _BINARY:
            return sql.SQL("({} " + _BINARY[kind] + " {})").format(
                emit(node.this), emit(node.expression)
            )
        if kind is exp.Not:
            return sql.SQL("(NOT {})").format(emit(node.this))
        if kind is exp.Paren:
            return sql.SQL("({})").format(emit(node.this))
        if kind is exp.Exists:
            return sql.SQL("EXISTS ({})").format(emit(node.this))
        if kind is exp.In:
            query = node.args.get("query")
            if query:
                if node.expressions:
                    raise UnsafeQuery("invalid IN expression")
                return sql.SQL("({} IN {})").format(emit(node.this), emit(query))
            if not node.expressions:
                raise UnsafeQuery("empty IN expression")
            return sql.SQL("({} IN ({}))").format(
                emit(node.this), sql.SQL(", ").join(emit(n) for n in node.expressions)
            )
        if kind in _FUNCTIONS:
            return sql.SQL("pg_catalog." + _FUNCTIONS[kind] + "({})").format(emit(node.this))
        if kind is exp.Coalesce:
            return sql.SQL("COALESCE({})").format(
                sql.SQL(", ").join(emit(n) for n in [node.this, *node.expressions])
            )
        raise UnsafeQuery("unsupported SQL expression")


def compile_query(
    statement: str,
    schema: str,
    session_id: UUID,
    params: JsonObject | None,
) -> tuple[sql.Composed, dict[str, Any]]:
    if len(statement.encode()) > 16 * 1024:
        raise UnsafeQuery("SQL exceeds 16 KiB")
    try:
        statements = sqlglot.parse(statement, read="postgres")
    except (sqlglot.errors.SqlglotError, RecursionError) as exc:
        raise UnsafeQuery("invalid SELECT syntax") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise UnsafeQuery("exactly one SELECT is required")
    tree = statements[0]
    nodes = list(tree.walk())
    if len(nodes) > 256 or sum(isinstance(n, exp.Table) for n in nodes) > 4:
        raise UnsafeQuery("SQL complexity limit exceeded")
    compiler = _Compiler(params or {})
    # Every node and option is checked even when its parent has a specialized emitter.
    for node in nodes:
        kind = type(node)
        if kind not in _ALLOWED or any(
            value is not None and value is not False and value != [] and key not in _ALLOWED[kind]
            for key, value in node.args.items()
        ):
            raise UnsafeQuery("unsupported SQL node or option")
    try:
        rendered = compiler.render(tree)
    except (ValueError, TypeError, RecursionError) as exc:
        raise UnsafeQuery("invalid bounded SQL expression") from exc
    compiler.binds["_kapy_session"] = session_id
    compiler.binds["_kapy_kinds"] = list(HISTORY_KINDS)
    query = sql.SQL(
        "WITH _kapy_history AS MATERIALIZED "
        "(SELECT seq, run_id, kind, message_id, text, data, created_at FROM {}.records "
        "WHERE session_id = %(_kapy_session)s AND kind = ANY(%(_kapy_kinds)s)) {}"
    ).format(sql.Identifier(schema), rendered)
    return query, compiler.binds
