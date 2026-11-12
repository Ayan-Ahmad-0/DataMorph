"""PostgreSQL direct database connector powered by DuckDB postgres extension."""

from __future__ import annotations
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import duckdb
from pydantic import BaseModel, Field, field_validator
import sqlglot
from sqlglot import exp

from core.security import escape_sql_literal, validate_external_host
from connectors.base import (
    BaseConnector,
    ConnectionTestResult,
    ConnectorMetadata,
    ConnectorType,
    ExportMode,
    SyncResult,
)

logger = logging.getLogger(__name__)


class PostgresConnectorConfig(BaseModel):
    """Configuration for connecting to a PostgreSQL database."""
    host: str = Field(..., description="PostgreSQL host (e.g., localhost, aws-0-us-east-1.pooler.supabase.com)")
    port: int = Field(5432, description="PostgreSQL port (default 5432)")
    database: str = Field(..., description="Target database name")
    username: str = Field(..., description="Database user name")
    password: str = Field(..., description="Database password")
    schema_name: str = Field("public", description="Target database schema name")
    table_name: str = Field(default="", description="Target destination or source table name")
    query: Optional[str] = Field(None, description="Optional custom SQL SELECT query for import")
    sslmode: str = Field("prefer", description="SSL connection mode (disable, allow, prefer, require)")
    mode: ExportMode = Field(ExportMode.OVERWRITE, description="Export mode: overwrite or append")

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, v: Any) -> ExportMode:
        if isinstance(v, ExportMode):
            return v
        if isinstance(v, str):
            v_clean = v.strip().lower()
            if v_clean == "append":
                return ExportMode.APPEND
            return ExportMode.OVERWRITE
        return ExportMode.OVERWRITE


class PostgresConnector(BaseConnector):
    """High-speed vectorized database connector for PostgreSQL using DuckDB."""

    connector_type = ConnectorType.POSTGRES
    display_name = "PostgreSQL"
    description = "Push cleaned tables directly into PostgreSQL or cloud Postgres services (Supabase, Neon, RDS)."
    category = "database"

    def get_metadata(self) -> ConnectorMetadata:
        return ConnectorMetadata(
            id=self.connector_type.value,
            display_name=self.display_name,
            description=self.description,
            category=self.category,
            supported_formats=["table"],
            supports_import=True,
            supports_export=True,
            config_fields=[
                {"name": "host", "label": "Host", "type": "string", "required": True, "placeholder": "localhost or db.xyz.supabase.co"},
                {"name": "port", "label": "Port", "type": "number", "required": False, "default": 5432},
                {"name": "database", "label": "Database", "type": "string", "required": True, "placeholder": "postgres"},
                {"name": "username", "label": "Username", "type": "string", "required": True, "placeholder": "postgres"},
                {"name": "password", "label": "Password", "type": "password", "required": True, "secret": True},
                {"name": "schema_name", "label": "Schema", "type": "string", "required": False, "default": "public", "placeholder": "public"},
                {"name": "table_name", "label": "Table Name", "type": "string", "required": True, "placeholder": "cleaned_dataset"},
                {"name": "mode", "label": "Write Mode", "type": "select", "options": ["overwrite", "append"], "default": "overwrite"},
                {"name": "sslmode", "label": "SSL Mode", "type": "select", "options": ["prefer", "require", "disable"], "default": "prefer"},
            ],
        )

    def _sanitize_error_message(self, message: str, password: Optional[str] = None) -> str:
        """Strip raw passwords and sensitive credentials from exception messages before sending to client."""
        msg = str(message)
        if password and len(str(password).strip()) > 1:
            msg = msg.replace(str(password), "••••••••")
        # Mask password in libpq connection string: password='...' or password="..." or password=...
        msg = re.sub(r"password=['\"][^'\"]*['\"]", "password='••••••••'", msg, flags=re.IGNORECASE)
        msg = re.sub(r"password=[^\s,;'\"]+", "password=••••••••", msg, flags=re.IGNORECASE)
        # Mask URI user:password@host
        msg = re.sub(r"://([^:/]+):([^@]+)@", r"://\1:••••••••@", msg)

        # Contextual troubleshooting hint for Supabase IPv6 unreachable
        if "supabase.co" in msg and ("Network is unreachable" in msg or "2406:" in msg or "Network unreachable" in msg):
            msg += "\n\n💡 Supabase Fix: Direct host 'db.[ref].supabase.co' is IPv6-only. Your network appears to be IPv4-only. In Supabase Dashboard -> Connect, select 'Session Pooler' to get your IPv4 hostname (e.g. aws-0-[region].pooler.supabase.com with port 5432 or 6543)."

        return msg

    def _sanitize_identifier(self, identifier: str, field_name: str) -> str:
        """Ensure SQL identifier is alphanumeric with underscores to prevent injection."""
        clean = identifier.strip()
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", clean):
            raise ValueError(f"Invalid {field_name} '{identifier}'. Must contain only letters, numbers, and underscores, starting with a letter or underscore.")
        return clean

    def _build_connection_string(self, cfg: PostgresConnectorConfig) -> str:
        """Construct a safe libpq connection string with SSRF and sslmode validation."""
        host_val = cfg.host.strip()
        port_val = int(cfg.port)
        user_val = cfg.username
        pwd_val = cfg.password
        db_val = cfg.database

        # If user pasted a full postgresql:// URL into host
        if host_val.startswith("postgres://") or host_val.startswith("postgresql://"):
            try:
                from urllib.parse import urlparse, unquote
                parsed = urlparse(host_val)
                if parsed.hostname:
                    host_val = parsed.hostname
                if parsed.port:
                    port_val = parsed.port
                if parsed.username:
                    user_val = unquote(parsed.username)
                if parsed.password:
                    pwd_val = unquote(parsed.password)
                if parsed.path and parsed.path.strip("/"):
                    db_val = unquote(parsed.path.strip("/"))
            except Exception:
                pass

        host_val = validate_external_host(host_val)
        allowed_ssl = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
        sslmode_val = cfg.sslmode.strip().lower()
        if sslmode_val not in allowed_ssl:
            raise ValueError(f"Unsupported sslmode '{cfg.sslmode}'. Allowed: {sorted(allowed_ssl)}")

        # Escape single quotes and backslashes in password/user if necessary
        pwd_escaped = pwd_val.replace("\\", "\\\\").replace("'", "\\'")
        user_escaped = user_val.replace("\\", "\\\\").replace("'", "\\'")
        db_escaped = db_val.replace("\\", "\\\\").replace("'", "\\'")
        host_escaped = host_val.replace("\\", "\\\\").replace("'", "\\'")

        return (
            f"host='{host_escaped}' port={port_val} dbname='{db_escaped}' "
            f"user='{user_escaped}' password='{pwd_escaped}' sslmode='{sslmode_val}'"
        )

    def _validate_import_query(self, raw_query: str) -> str:
        """Strip trailing semicolons and verify query is a single read-only SELECT without local file reads."""
        clean_query = re.sub(r"[;\s]+$", "", (raw_query or "").strip())
        if not clean_query:
            raise ValueError("Custom Postgres import query cannot be empty.")

        stmts = duckdb.extract_statements(clean_query)
        if len(stmts) != 1 or stmts[0].type != duckdb.StatementType.SELECT:
            raise ValueError("Custom Postgres import query must be a single read-only SELECT statement.")

        parsed = sqlglot.parse(clean_query, read="postgres")
        if not parsed or len(parsed) != 1 or not isinstance(parsed[0], (exp.Select, exp.Union)):
            raise ValueError("Custom Postgres import query must be a single SELECT or UNION query.")

        blocked_prefixes = (
            "read_",
            "readcsv",
            "readparquet",
            "readjson",
            "readtext",
            "readblob",
            "readxlsx",
            "http",
            "duckdb_",
        )
        blocked_exact = {"glob", "parquet_scan", "parquet_metadata", "sniff_csv", "sqlite_scan", "current_setting"}
        for fn in parsed[0].find_all(exp.Func):
            fn_name = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name() or "").lower()
            if fn_name in blocked_exact or fn_name.startswith(blocked_prefixes):
                raise ValueError(f"Function '{fn_name}' is not allowed inside custom Postgres import queries.")

        return clean_query

    def test_connection(self, config: Dict[str, Any]) -> ConnectionTestResult:
        """Attempt to attach to PostgreSQL and execute a ping query."""
        start_time = time.perf_counter()
        try:
            cfg = PostgresConnectorConfig.model_validate(config)
            if cfg.schema_name:
                self._sanitize_identifier(cfg.schema_name, "schema_name")
            if cfg.table_name:
                self._sanitize_identifier(cfg.table_name, "table_name")

            con = duckdb.connect(":memory:")
            con.execute("INSTALL postgres; LOAD postgres;")
            conn_str = self._build_connection_string(cfg)
            conn_str_sql = conn_str.replace("'", "''")

            alias = f"pg_test_{int(time.time())}"
            con.execute(f"ATTACH '{conn_str_sql}' AS {alias} (TYPE POSTGRES);")
            
            # Discover existing tables in the schema, falling back to all user schemas
            tables: List[str] = []
            try:
                schema_clean = cfg.schema_name or "public"
                tables_res = con.execute(
                    f"SELECT table_name FROM {alias}.information_schema.tables "
                    f"WHERE table_schema = '{schema_clean}' AND table_type = 'BASE TABLE' LIMIT 25;"
                ).fetchall()
                if tables_res:
                    tables = [str(r[0]) for r in tables_res]
                else:
                    # If specified schema has no tables, discover tables across all non-system schemas
                    fallback_res = con.execute(
                        f"SELECT table_schema, table_name FROM {alias}.information_schema.tables "
                        f"WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast', 'extensions', 'graphql', 'graphql_public', 'pgbouncer') "
                        f"AND table_type = 'BASE TABLE' LIMIT 30;"
                    ).fetchall()
                    if fallback_res:
                        tables = [f"{r[0]}.{r[1]}" for r in fallback_res]
            except Exception:
                con.execute(f"SELECT 1 FROM {alias}.information_schema.tables LIMIT 1;")

            con.execute(f"DETACH {alias};")
            con.close()

            elapsed = (time.perf_counter() - start_time) * 1000
            msg = f"Successfully connected to PostgreSQL at {cfg.host}:{cfg.port}/{cfg.database}"
            if tables:
                msg += f" | Available tables ({len(tables)}): {', '.join(tables)}"
            else:
                msg += " | Note: No user tables found yet in schema."

            return ConnectionTestResult(
                success=True,
                message=msg,
                latency_ms=round(elapsed, 2),
                details={"host": cfg.host, "database": cfg.database, "schema": cfg.schema_name, "tables": tables},
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start_time) * 1000
            safe_err = self._sanitize_error_message(str(e), password=config.get("password"))
            logger.warning("Postgres connection test failed: %s", safe_err)
            return ConnectionTestResult(
                success=False,
                message=f"PostgreSQL connection failed: {safe_err}",
                latency_ms=round(elapsed, 2),
            )

    def export_dataset(
        self,
        source_parquet_path: Path,
        config: Dict[str, Any],
        options: Optional[Dict[str, Any]] = None,
    ) -> SyncResult:
        """Export local Parquet dataset into a PostgreSQL table."""
        if not source_parquet_path.exists():
            raise FileNotFoundError(f"Source dataset not found at {source_parquet_path}")

        start_time = time.perf_counter()
        cfg = PostgresConnectorConfig.model_validate(config)
        if not cfg.table_name:
            raise ValueError("Table name is required for PostgreSQL export.")
        schema = self._sanitize_identifier(cfg.schema_name, "schema_name")
        table = self._sanitize_identifier(cfg.table_name, "table_name")

        con = duckdb.connect(":memory:")
        con.execute("INSTALL postgres; LOAD postgres;")

        conn_str = self._build_connection_string(cfg)
        conn_str_sql = conn_str.replace("'", "''")
        alias = "pg_target"

        try:
            con.execute(f"ATTACH '{conn_str_sql}' AS {alias} (TYPE POSTGRES);")

            src_path_str = str(source_parquet_path.resolve()).replace("\\", "/")
            row_count_res = con.execute(f"SELECT count(*) FROM read_parquet('{src_path_str}')").fetchone()
            row_count = int(row_count_res[0]) if row_count_res else 0

            # Execute table creation / insertion
            if cfg.mode == ExportMode.OVERWRITE:
                con.execute(f"DROP TABLE IF EXISTS {alias}.{schema}.{table};")
                con.execute(f"CREATE TABLE {alias}.{schema}.{table} AS SELECT * FROM read_parquet('{src_path_str}');")
            elif cfg.mode == ExportMode.APPEND:
                con.execute(f"INSERT INTO {alias}.{schema}.{table} SELECT * FROM read_parquet('{src_path_str}');")
            else:
                raise ValueError(f"Unsupported write mode: {cfg.mode}")

            destination_uri = f"postgresql://{cfg.host}:{cfg.port}/{cfg.database}/{schema}.{table}"
            elapsed = time.perf_counter() - start_time

            return SyncResult(
                success=True,
                rows_transferred=row_count,
                destination_uri=destination_uri,
                elapsed_seconds=round(elapsed, 3),
                message=f"Successfully transferred {row_count:,} rows into {schema}.{table} in {elapsed:.2f}s",
                details={
                    "host": cfg.host,
                    "database": cfg.database,
                    "schema": schema,
                    "table": table,
                    "mode": cfg.mode.value,
                },
            )
        except Exception as e:
            safe_err = self._sanitize_error_message(str(e), password=cfg.password)
            logger.exception("Postgres export failed to %s.%s: %s", schema, table, safe_err)
            raise RuntimeError(f"Failed to export to PostgreSQL table '{schema}.{table}': {safe_err}") from None
        finally:
            try:
                con.execute(f"DETACH {alias};")
            except Exception:
                pass
            con.close()

    def import_dataset(
        self,
        config: Dict[str, Any],
        target_parquet_path: Path,
        options: Optional[Dict[str, Any]] = None,
    ) -> SyncResult:
        """Import table or query from PostgreSQL directly into canonical local Parquet."""
        start_time = time.perf_counter()
        cfg = PostgresConnectorConfig.model_validate(config)

        import_query = cfg.query or (options or {}).get("query")
        if import_query:
            source_desc = "custom query"
        else:
            if not cfg.table_name:
                raise ValueError("Either table_name or query must be provided for PostgreSQL import.")
            schema = self._sanitize_identifier(cfg.schema_name, "schema_name")
            table = self._sanitize_identifier(cfg.table_name, "table_name")
            source_desc = f"{schema}.{table}"

        target_parquet_path.parent.mkdir(parents=True, exist_ok=True)
        target_path_str = str(target_parquet_path.resolve()).replace("\\", "/")

        con = duckdb.connect(":memory:")
        con.execute("INSTALL postgres; LOAD postgres;")

        conn_str = self._build_connection_string(cfg)
        conn_str_sql = conn_str.replace("'", "''")
        alias = "pg_source"

        try:
            con.execute(f"ATTACH '{conn_str_sql}' AS {alias} (TYPE POSTGRES);")
            try:
                con.execute(f"USE {alias};")
            except Exception:
                pass

            if import_query:
                clean_query = self._validate_import_query(import_query)
                source_expr = f"({clean_query})"
            else:
                source_expr = f"{alias}.{schema}.{table}"

            con.execute(
                f"COPY (SELECT * FROM {source_expr}) TO '{escape_sql_literal(target_path_str)}' "
                "(FORMAT PARQUET, COMPRESSION 'ZSTD');"
            )

            row_count_res = con.execute(f"SELECT count(*) FROM read_parquet('{target_path_str}')").fetchone()
            row_count = int(row_count_res[0]) if row_count_res else 0

            elapsed = time.perf_counter() - start_time
            destination_uri = f"postgresql://{cfg.host}:{cfg.port}/{cfg.database}/{source_desc}"

            return SyncResult(
                success=True,
                rows_transferred=row_count,
                destination_uri=destination_uri,
                elapsed_seconds=round(elapsed, 3),
                message=f"Successfully imported {row_count:,} rows from PostgreSQL {source_desc} in {elapsed:.2f}s",
                details={
                    "host": cfg.host,
                    "database": cfg.database,
                    "source": source_desc,
                    "target_path": str(target_parquet_path),
                },
            )
        except Exception as e:
            safe_err = self._sanitize_error_message(str(e), password=cfg.password)
            logger.exception("Postgres import failed: %s", safe_err)
            raise RuntimeError(f"Failed to import from PostgreSQL: {safe_err}") from None
        finally:
            try:
                con.execute(f"DETACH {alias};")
            except Exception:
                pass
            con.close()

