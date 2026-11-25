"""SQL dialect transpilation and compatibility checks backed by sqlglot."""

from __future__ import annotations

from typing import List, Tuple

import sqlglot
from sqlglot import expressions as exp

from core.export.models import SqlDialect

_STRFTIME_TO_SQL_MAP = (
    ("%Y", "YYYY"),
    ("%m", "MM"),
    ("%d", "DD"),
    ("%H", "HH24"),
    ("%M", "MI"),
    ("%S", "SS"),
)


def strftime_to_sql_format(fmt: str) -> Tuple[str, bool]:
    """Convert a Python/DuckDB strptime format string to SQL TO_TIMESTAMP format."""
    converted = fmt
    has_tz = "%z" in converted or "%Z" in converted or converted.endswith("Z")
    for src, dst in _STRFTIME_TO_SQL_MAP:
        converted = converted.replace(src, dst)
    converted = converted.replace("T", '"T"') if '"T"' not in converted and "T" in converted else converted
    converted = converted.replace("%z", "TZH:TZM").replace("Z", '"Z"')
    return converted, has_tz


def _transform_ast_for_dialect(tree: exp.Expression, target_dialect: SqlDialect) -> List[str]:
    """Apply targeted AST rewrites so DuckDB SQL transpiles cleanly to target warehouses."""
    warnings: List[str] = []
    seen_warnings: set[str] = set()

    def _warn(msg: str) -> None:
        if msg not in seen_warnings:
            seen_warnings.add(msg)
            warnings.append(msg)

    for node in list(tree.walk()):
        # 1. try_strptime(expr, fmt)
        if isinstance(node, exp.Anonymous) and node.this.lower() == "try_strptime":
            args = node.expressions
            if len(args) == 2:
                arg_expr, fmt_node = args[0], args[1]
                fmt_str = fmt_node.this if isinstance(fmt_node, exp.Literal) else str(fmt_node)
                if target_dialect == "snowflake":
                    sql_fmt, has_tz = strftime_to_sql_format(fmt_str)
                    fn = "TRY_TO_TIMESTAMP_TZ" if has_tz else "TRY_TO_TIMESTAMP"
                    node.replace(
                        exp.Anonymous(this=fn, expressions=[arg_expr.copy(), exp.Literal.string(sql_fmt)])
                    )
                elif target_dialect == "bigquery":
                    node.replace(
                        exp.Anonymous(
                            this="SAFE.PARSE_TIMESTAMP",
                            expressions=[exp.Literal.string(fmt_str), arg_expr.copy()],
                        )
                    )
                elif target_dialect == "postgres":
                    sql_fmt, _ = strftime_to_sql_format(fmt_str)
                    node.replace(
                        exp.Anonymous(
                            this="TO_TIMESTAMP",
                            expressions=[arg_expr.copy(), exp.Literal.string(sql_fmt)],
                        )
                    )
                    _warn(
                        "PostgreSQL TO_TIMESTAMP raises an error on unparseable date strings "
                        "(unlike DuckDB try_strptime which returns NULL)."
                    )

        # 2. TryCast caveats
        elif isinstance(node, exp.TryCast):
            if target_dialect == "postgres":
                _warn(
                    "PostgreSQL has no TRY_CAST; generated CAST expressions will fail on "
                    "non-conforming string values."
                )
            elif target_dialect == "snowflake":
                # Preserve TRY_CAST when casting string columns (Snowflake supports TRY_CAST on VARCHAR)
                _warn(
                    "Snowflake transpiles try_cast to CAST for compatibility with numeric inputs; "
                    "verify unvalidated string columns if malformed values remain."
                )

        # 3. RegexpReplace modifier cleanup for BigQuery
        elif isinstance(node, exp.RegexpReplace) and target_dialect == "bigquery":
            if node.args.get("modifiers") is not None:
                node.set("modifiers", None)

        # 4. SHA256 differences for PII hashing
        elif isinstance(node, exp.SHA2):
            if target_dialect == "bigquery":
                node.replace(exp.Anonymous(this="TO_HEX", expressions=[node.copy()]))
            elif target_dialect == "postgres":
                inner = node.this.copy()
                utf8_bytes = exp.Anonymous(
                    this="convert_to",
                    expressions=[inner, exp.Literal.string("UTF8")],
                )
                sha_bytes = exp.Anonymous(this="sha256", expressions=[utf8_bytes])
                node.replace(
                    exp.Anonymous(this="encode", expressions=[sha_bytes, exp.Literal.string("hex")])
                )

        # 5. MEDIAN / MODE across BigQuery & Postgres
        elif isinstance(node, exp.Median):
            if target_dialect == "bigquery":
                col_expr = node.this.copy()
                approx = exp.Anonymous(
                    this="APPROX_QUANTILES",
                    expressions=[col_expr, exp.Literal.number(2)],
                )
                offset_expr = exp.Bracket(
                    this=approx,
                    expressions=[exp.Anonymous(this="OFFSET", expressions=[exp.Literal.number(1)])],
                )
                node.replace(offset_expr)
                _warn(
                    "BigQuery uses APPROX_QUANTILES(col, 2)[OFFSET(1)] for fill_median, "
                    "which is approximate on very large datasets."
                )
        elif isinstance(node, exp.Mode):
            if target_dialect == "postgres":
                col_expr = node.this.copy()
                wg = exp.WithinGroup(
                    this=exp.Mode(),
                    expression=exp.Order(expressions=[exp.Ordered(this=col_expr)]),
                )
                node.replace(wg)
            elif target_dialect == "bigquery":
                col_expr = node.this.copy()
                top_cnt = exp.Anonymous(
                    this="APPROX_TOP_COUNT",
                    expressions=[col_expr, exp.Literal.number(1)],
                )
                elem = exp.Bracket(
                    this=top_cnt,
                    expressions=[exp.Anonymous(this="OFFSET", expressions=[exp.Literal.number(0)])],
                )
                node.replace(exp.Dot(this=elem, expression=exp.to_identifier("value")))
                _warn(
                    "BigQuery uses APPROX_TOP_COUNT(col, 1)[OFFSET(0)].value for fill_mode."
                )

        # 6. QUALIFY rewrite notice for Postgres
        elif isinstance(node, exp.Qualify) and target_dialect == "postgres":
            _warn(
                "PostgreSQL does not support QUALIFY; deduplication was rewritten as a "
                "ROW_NUMBER() subquery."
            )

        # 7. SELECT * REPLACE warning for Postgres if full column list unavailable
        elif isinstance(node, exp.Star) and node.args.get("replace") and target_dialect == "postgres":
            _warn(
                "PostgreSQL does not support SELECT * REPLACE (...); provide dataset profile "
                "columns to emit explicit column projections."
            )

    return warnings


def transpile_duckdb_sql(sql: str, target_dialect: SqlDialect) -> Tuple[str, List[str]]:
    """Transpile DuckDB SQL to target_dialect via sqlglot and return (sql, warnings)."""
    if target_dialect == "duckdb":
        sqlglot.parse_one(sql, read="duckdb")
        return sql, []

    tree = sqlglot.parse_one(sql, read="duckdb")
    warnings = _transform_ast_for_dialect(tree, target_dialect)

    rendered = tree.sql(dialect=target_dialect, pretty=True)
    if not rendered.rstrip().endswith(";"):
        rendered = rendered.rstrip() + ";"

    # Verify generated SQL parses cleanly in the target dialect
    sqlglot.parse_one(rendered.rstrip(";"), read=target_dialect)
    return rendered, warnings
