"""Service managing multi-table modeling sessions, ingestion, and ERD generation."""

from __future__ import annotations
import json
import logging
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import duckdb

from core.modeling.detector import RelationshipDetector
from core.security import resolve_within_base, validate_safe_id
from core.modeling.models import (
    ColumnMetadata,
    JoinPreviewResult,
    ModelingSessionState,
    Relationship,
    TableMetadata,
)
from formats import convert_to_parquet, detect_format

logger = logging.getLogger(__name__)


def sanitize_table_name(name: str) -> str:
    """Sanitize string into a safe SQL table identifier."""
    stem = Path(name).stem
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", stem).lower()
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned


class ModelingService:
    """Orchestrates multi-table sessions, DuckDB storage, relationship inference, and ERD exports."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir or "data/modeling").resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _find_session(self, session_id: str) -> Optional[tuple[Path, str]]:
        """Locate an existing modeling session across tenant partitions or legacy flat directory."""
        # Check tenant partition
        if self.base_dir.exists():
            for child in self.base_dir.iterdir():
                if child.is_dir():
                    state_candidate = child / session_id / "state.json"
                    if state_candidate.exists():
                        return child / session_id, child.name

        # Check legacy flat directory
        legacy_state = self.base_dir / session_id / "state.json"
        if legacy_state.exists():
            return self.base_dir / session_id, "default"

        return None

    def _get_session_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        uid = user_id or "default"
        # If session already exists in legacy or tenant dir, return it
        found = self._find_session(session_id)
        if found:
            s_dir, owner = found
            if user_id is not None and owner != user_id:
                raise PermissionError(f"User '{user_id}' does not have access to modeling session '{session_id}'")
            return s_dir

        s_dir = self.base_dir / uid / session_id
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "tables").mkdir(parents=True, exist_ok=True)
        return s_dir

    def _get_state_file(self, session_id: str, user_id: Optional[str] = None) -> Path:
        return self._get_session_dir(session_id, user_id=user_id) / "state.json"

    def _load_state(self, session_id: str, user_id: Optional[str] = None) -> ModelingSessionState:
        uid = user_id or "default"
        found = self._find_session(session_id)
        if not found:
            raise KeyError(f"Modeling session '{session_id}' not found.")

        session_dir, owner = found
        if user_id is not None and owner != uid:
            raise PermissionError(f"User '{user_id}' does not have access to modeling session '{session_id}'")

        state_file = session_dir / "state.json"
        if state_file.exists():
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = ModelingSessionState(**data)
            if user_id is not None and getattr(state, "user_id", "default") != uid:
                raise PermissionError(f"User '{user_id}' does not have access to modeling session '{session_id}'")
            return state

        raise KeyError(f"Modeling session state for '{session_id}' is missing.")

    def _save_state(self, session_id: str, state: ModelingSessionState, user_id: Optional[str] = None) -> None:
        uid = user_id or getattr(state, "user_id", "default")
        state_file = self._get_state_file(session_id, user_id=uid)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state.model_dump(), f, indent=2)

    def _build_duckdb_connection(self, session_id: str, user_id: Optional[str] = None) -> duckdb.DuckDBPyConnection:
        """Create an in-memory DuckDB connection with views for all session tables."""
        con = duckdb.connect(":memory:")
        tables_dir = self._get_session_dir(session_id, user_id=user_id) / "tables"

        for p_file in tables_dir.glob("*.parquet"):
            tbl_name = p_file.stem
            con.execute(f'CREATE VIEW "{tbl_name}" AS SELECT * FROM read_parquet(\'{p_file.as_posix()}\')')

        return con

    def create_session(self, user_id: str = "default") -> str:
        """Initialize a new multi-table modeling session bound to a tenant."""
        session_id = f"model_{uuid.uuid4().hex[:8]}"
        self._get_session_dir(session_id, user_id=user_id)
        state = ModelingSessionState(
            session_id=session_id,
            user_id=user_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._save_state(session_id, state, user_id=user_id)
        return session_id

    def get_session_state(self, session_id: str, user_id: Optional[str] = None) -> ModelingSessionState:
        """Retrieve current state of a modeling session."""
        return self._load_state(session_id, user_id=user_id)

    def add_table(
        self,
        session_id: str,
        file_path: Path,
        original_filename: str,
        table_name: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> TableMetadata:
        """Stage a single data file (CSV, Parquet, JSON, Excel) into canonical Parquet and register it."""
        state = self._load_state(session_id, user_id=user_id)
        clean_name = sanitize_table_name(table_name or original_filename)

        # Avoid collision
        if clean_name in state.tables and (table_name is None):
            clean_name = f"{clean_name}_{len(state.tables) + 1}"

        target_parquet = self._get_session_dir(session_id, user_id=state.user_id) / "tables" / f"{clean_name}.parquet"

        # Convert to flat canonical Parquet
        fmt_id, _ = detect_format(file_path)
        convert_to_parquet(file_path, target_parquet, format_id=fmt_id)

        # Inspect table metadata via DuckDB
        con = duckdb.connect(":memory:")
        desc = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{target_parquet.as_posix()}')").fetchall()
        row_count = con.execute(f"SELECT count(*) FROM read_parquet('{target_parquet.as_posix()}')").fetchone()[0]
        sample_rows = con.execute(f"SELECT * FROM read_parquet('{target_parquet.as_posix()}') LIMIT 3").fetchall()

        columns: List[ColumnMetadata] = []
        for idx, row in enumerate(desc):
            col_name = row[0]
            col_type = row[1]
            samples = [r[idx] for r in sample_rows if r[idx] is not None]
            columns.append(
                ColumnMetadata(
                    name=col_name,
                    data_type=col_type,
                    is_nullable=(row[2] == "YES" if len(row) > 2 else True),
                    sample_values=samples[:3],
                )
            )

        file_size = target_parquet.stat().st_size if target_parquet.exists() else 0

        # Calculate a pleasant grid offset for canvas position
        idx = len(state.tables)
        col_offset = (idx % 3) * 340 + 40
        row_offset = (idx // 3) * 360 + 40

        table_meta = TableMetadata(
            table_name=clean_name,
            row_count=row_count,
            file_size_bytes=file_size,
            original_filename=original_filename,
            columns=columns,
            position_x=float(col_offset),
            position_y=float(row_offset),
        )

        state.tables[clean_name] = table_meta

        # Detect PKs and run cross-table relationship discovery
        session_con = self._build_duckdb_connection(session_id, user_id=state.user_id)
        RelationshipDetector.detect_primary_keys(session_con, state.tables)
        detected_rels = RelationshipDetector.detect_foreign_keys(session_con, state.tables)

        # Merge relationships (preserve confirmed/manual ones)
        existing_keys = {
            (r.from_table, r.from_column, r.to_table, r.to_column): r
            for r in state.relationships
        }
        for dr in detected_rels:
            k = (dr.from_table, dr.from_column, dr.to_table, dr.to_column)
            if k not in existing_keys:
                state.relationships.append(dr)

        self._save_state(session_id, state, user_id=state.user_id)
        return table_meta

    def add_sqlite_database(self, session_id: str, file_path: Path, user_id: Optional[str] = None) -> List[TableMetadata]:
        """Extract and stage all tables and foreign keys from an SQLite database."""
        state = self._load_state(session_id, user_id=user_id)
        added_tables: List[TableMetadata] = []

        try:
            sqlite_conn = sqlite3.connect(file_path)
            cursor = sqlite_conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            tbl_names = [r[0] for r in cursor.fetchall()]

            # 1. Export each table to Parquet
            duck_con = duckdb.connect(":memory:")
            try:
                duck_con.execute("INSTALL sqlite; LOAD sqlite;")
            except Exception:
                pass

            pos_idx = len(state.tables)
            for t_name in tbl_names:
                clean_name = sanitize_table_name(t_name)
                target_parquet = self._get_session_dir(session_id, user_id=state.user_id) / "tables" / f"{clean_name}.parquet"

                # Direct dump via DuckDB or pandas
                duck_con.execute(f"ATTACH '{file_path.as_posix()}' AS sqldb (TYPE sqlite);")
                duck_con.execute(f"""
                    COPY (SELECT * FROM sqldb."{t_name}") 
                    TO '{target_parquet.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD);
                """)
                duck_con.execute("DETACH sqldb;")

                # Inspect metadata
                desc = duck_con.execute(f"DESCRIBE SELECT * FROM read_parquet('{target_parquet.as_posix()}')").fetchall()
                row_count = duck_con.execute(f"SELECT count(*) FROM read_parquet('{target_parquet.as_posix()}')").fetchone()[0]

                columns: List[ColumnMetadata] = []
                for idx, row in enumerate(desc):
                    columns.append(
                        ColumnMetadata(
                            name=row[0],
                            data_type=row[1],
                            is_nullable=(row[2] == "YES" if len(row) > 2 else True),
                        )
                    )

                col_offset = (pos_idx % 3) * 340 + 40
                row_offset = (pos_idx // 3) * 360 + 40
                pos_idx += 1

                t_meta = TableMetadata(
                    table_name=clean_name,
                    row_count=row_count,
                    file_size_bytes=target_parquet.stat().st_size,
                    original_filename=file_path.name,
                    columns=columns,
                    position_x=float(col_offset),
                    position_y=float(row_offset),
                )
                state.tables[clean_name] = t_meta
                added_tables.append(t_meta)

            # 2. Extract native SQLite Foreign Keys
            for t_name in tbl_names:
                child_clean = sanitize_table_name(t_name)
                cursor.execute(f'PRAGMA foreign_key_list("{t_name}")')
                # Returns (id, seq, table, from, to, on_update, on_delete, match)
                fk_rows = cursor.fetchall()
                for fk in fk_rows:
                    parent_table = sanitize_table_name(fk[2])
                    from_col = fk[3]
                    to_col = fk[4] or "id"

                    if child_clean in state.tables and parent_table in state.tables:
                        rel_id = f"rel_{child_clean}_{from_col}_{parent_table}_{to_col}"
                        state.relationships.append(
                            Relationship(
                                id=rel_id,
                                from_table=child_clean,
                                from_column=from_col,
                                to_table=parent_table,
                                to_column=to_col,
                                cardinality="1:N",
                                confidence=1.0,
                                match_ratio=1.0,
                                orphan_count=0,
                                status="confirmed",
                            )
                        )

            sqlite_conn.close()

            # 3. Detect PKs and remaining relationships
            session_con = self._build_duckdb_connection(session_id, user_id=state.user_id)
            RelationshipDetector.detect_primary_keys(session_con, state.tables)
            detected_rels = RelationshipDetector.detect_foreign_keys(session_con, state.tables)

            existing_keys = {
                (r.from_table, r.from_column, r.to_table, r.to_column): r
                for r in state.relationships
            }
            for dr in detected_rels:
                k = (dr.from_table, dr.from_column, dr.to_table, dr.to_column)
                if k not in existing_keys:
                    state.relationships.append(dr)

            self._save_state(session_id, state, user_id=state.user_id)
            return added_tables
        except Exception as e:
            logger.error(f"Failed to extract SQLite database {file_path}: {e}")
            raise e

    def remove_table(self, session_id: str, table_name: str, user_id: Optional[str] = None) -> None:
        """Remove a staged table and any associated relationships."""
        state = self._load_state(session_id, user_id=user_id)
        if table_name in state.tables:
            del state.tables[table_name]

        target_parquet = self._get_session_dir(session_id, user_id=state.user_id) / "tables" / f"{table_name}.parquet"
        if target_parquet.exists():
            target_parquet.unlink()

        # Purge relationships involving this table
        state.relationships = [
            r for r in state.relationships
            if r.from_table != table_name and r.to_table != table_name
        ]

        self._save_state(session_id, state, user_id=state.user_id)

    def update_table_position(self, session_id: str, table_name: str, x: float, y: float, user_id: Optional[str] = None) -> None:
        """Update canvas coordinates of a table entity card."""
        state = self._load_state(session_id, user_id=user_id)
        if table_name in state.tables:
            state.tables[table_name].position_x = x
            state.tables[table_name].position_y = y
            self._save_state(session_id, state, user_id=state.user_id)

    def detect_relationships(self, session_id: str, user_id: Optional[str] = None) -> List[Relationship]:
        """Trigger relationship discovery across all staged tables."""
        state = self._load_state(session_id, user_id=user_id)
        con = self._build_duckdb_connection(session_id, user_id=state.user_id)

        RelationshipDetector.detect_primary_keys(con, state.tables)
        new_rels = RelationshipDetector.detect_foreign_keys(con, state.tables)

        existing_manuals = {r.id: r for r in state.relationships if r.status in ("confirmed", "manual")}
        merged: List[Relationship] = list(existing_manuals.values())

        for nr in new_rels:
            if nr.id not in existing_manuals:
                merged.append(nr)

        state.relationships = merged
        self._save_state(session_id, state, user_id=state.user_id)
        return merged

    def add_or_update_relationship(self, session_id: str, rel: Relationship, user_id: Optional[str] = None) -> Relationship:
        """Create or update a relationship."""
        state = self._load_state(session_id, user_id=user_id)
        state.relationships = [r for r in state.relationships if r.id != rel.id]
        state.relationships.append(rel)
        self._save_state(session_id, state, user_id=state.user_id)
        return rel

    def delete_relationship(self, session_id: str, rel_id: str, user_id: Optional[str] = None) -> None:
        """Delete an existing relationship by ID."""
        state = self._load_state(session_id, user_id=user_id)
        state.relationships = [r for r in state.relationships if r.id != rel_id]
        self._save_state(session_id, state, user_id=state.user_id)

    def preview_join(
        self,
        session_id: str,
        from_table: str,
        from_column: str,
        to_table: str,
        to_column: str,
        join_type: str = "INNER",
        user_id: Optional[str] = None,
    ) -> JoinPreviewResult:
        """Execute a join preview query between two tables and return statistics with sample rows."""
        state = self._load_state(session_id, user_id=user_id)
        con = self._build_duckdb_connection(session_id, user_id=state.user_id)
        join_type = join_type.upper()
        if join_type not in ("INNER", "LEFT", "RIGHT", "FULL"):
            join_type = "INNER"

        # Measure stats
        stats_query = f"""
            SELECT
                count(*) AS total_child_rows,
                count(c."{from_column}") FILTER (WHERE p."{to_column}" IS NOT NULL) AS matched_rows,
                count(c."{from_column}") FILTER (WHERE p."{to_column}" IS NULL) AS orphan_rows
            FROM "{from_table}" c
            LEFT JOIN "{to_table}" p ON c."{from_column}" = p."{to_column}"
        """
        stats = con.execute(stats_query).fetchone()
        total_child = stats[0] if stats else 0
        matched = stats[1] if stats else 0
        orphans = stats[2] if stats else 0
        match_ratio = round(matched / total_child, 3) if total_child > 0 else 0.0

        # Sample rows query
        sample_query = f"""
            SELECT c.*, p.*
            FROM "{from_table}" c
            {join_type} JOIN "{to_table}" p ON c."{from_column}" = p."{to_column}"
            LIMIT 10
        """
        rel = con.execute(sample_query)
        columns = [desc[0] for desc in rel.description] if rel.description else []
        raw_rows = rel.fetchall()
        records = [dict(zip(columns, row)) for row in raw_rows]

        return JoinPreviewResult(
            from_table=from_table,
            from_column=from_column,
            to_table=to_table,
            to_column=to_column,
            join_type=join_type,
            matched_rows=matched,
            total_child_rows=total_child,
            match_ratio=match_ratio,
            orphan_count=orphans,
            columns=columns,
            sample_rows=records,
        )

    def export_schema(self, session_id: str, export_format: str = "mermaid", user_id: Optional[str] = None) -> str:
        """Export the relational model as Mermaid ERD, DBML, or SQL DDL."""
        state = self._load_state(session_id, user_id=user_id)

        if export_format.lower() == "mermaid":
            lines = ["erDiagram"]
            # Render relationships
            for r in state.relationships:
                # Mermaid syntax: TABLE_A ||--o{ TABLE_B : "references"
                card_symbol = "||--o{" if r.cardinality in ("1:N", "1:n") else "||--||"
                lines.append(f'    "{r.to_table.upper()}" {card_symbol} "{r.from_table.upper()}" : "{r.from_column}"')

            # Render tables
            for tbl_name, tbl in state.tables.items():
                lines.append(f'    "{tbl_name.upper()}" {{')
                for col in tbl.columns:
                    type_str = col.data_type.lower()
                    pk_fk = "PK" if col.is_pk else ("FK" if col.is_fk else "")
                    lines.append(f'        {type_str} {col.name} {pk_fk}'.strip())
                lines.append("    }")
            return "\n".join(lines)

        elif export_format.lower() == "dbml":
            lines = []
            for tbl_name, tbl in state.tables.items():
                lines.append(f"Table {tbl_name} {{")
                for col in tbl.columns:
                    notes = []
                    if col.is_pk:
                        notes.append("pk")
                    note_str = f" [{', '.join(notes)}]" if notes else ""
                    lines.append(f"  {col.name} {col.data_type.lower()}{note_str}")
                lines.append("}\n")

            for r in state.relationships:
                lines.append(f"Ref: {r.from_table}.{r.from_column} > {r.to_table}.{r.to_column}")

            return "\n".join(lines)

        elif export_format.lower() in ("sql", "ddl", "sql_ddl"):
            lines = ["-- Relational Schema DDL generated by Data-Cleaner", ""]
            for tbl_name, tbl in state.tables.items():
                lines.append(f'CREATE TABLE "{tbl_name}" (')
                col_defs = []
                for col in tbl.columns:
                    nullability = " NOT NULL" if not col.is_nullable else ""
                    pk = " PRIMARY KEY" if col.is_pk else ""
                    col_defs.append(f'    "{col.name}" {col.data_type}{nullability}{pk}')
                lines.append(",\n".join(col_defs))
                lines.append(");\n")

            for r in state.relationships:
                lines.append(
                    f'ALTER TABLE "{r.from_table}" ADD CONSTRAINT fk_{r.from_table}_{r.from_column} '
                    f'FOREIGN KEY ("{r.from_column}") REFERENCES "{r.to_table}" ("{r.to_column}");'
                )
            return "\n".join(lines)

        else:
            raise ValueError(f"Unsupported export format '{export_format}'. Expected mermaid, dbml, or sql_ddl.")

    def create_query_handoff(self, session_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Prepare a multi-table JOIN query string, materializing merged.parquet for SQL & AI exploration."""
        state = self._load_state(session_id, user_id=user_id)
        if not state.tables:
            return {
                "query_id": session_id,
                "filename": f"merged_{session_id}.parquet",
                "row_count": 0,
                "columns": [],
                "file_size_bytes": 0,
                "detected_format": "PARQUET",
                "sql": "SELECT 1;",
                "tables": [],
                "relationships_count": 0,
            }

        table_names = list(state.tables.keys())
        first_table = table_names[0]
        con = self._build_duckdb_connection(session_id, user_id=state.user_id)
        merged_pq = self._get_session_dir(session_id, user_id=state.user_id) / "merged.parquet"

        if not state.relationships:
            query = f'SELECT * FROM "{first_table}"'
            sql = 'SELECT * FROM data LIMIT 100;'
        else:
            base_tbl = state.relationships[0].from_table
            joins = []
            joined_tables = {base_tbl}
            for r in state.relationships:
                if r.to_table not in joined_tables:
                    joins.append(f'LEFT JOIN "{r.to_table}" ON "{r.from_table}"."{r.from_column}" = "{r.to_table}"."{r.to_column}"')
                    joined_tables.add(r.to_table)
                elif r.from_table not in joined_tables:
                    joins.append(f'LEFT JOIN "{r.from_table}" ON "{r.from_table}"."{r.from_column}" = "{r.to_table}"."{r.to_column}"')
                    joined_tables.add(r.from_table)
            query = f'SELECT * FROM "{base_tbl}" {" ".join(joins)}'
            sql = 'SELECT * FROM data LIMIT 100;'

        # Materialize merged parquet in session dir for exploration and AI Analyst
        con.execute(f"COPY ({query}) TO '{merged_pq.as_posix()}' (FORMAT PARQUET)")
        desc = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{merged_pq.as_posix()}')").fetchall()
        row_count = con.execute(f"SELECT count(*) FROM read_parquet('{merged_pq.as_posix()}')").fetchone()[0]
        cols = [r[0] for r in desc]
        file_size = merged_pq.stat().st_size if merged_pq.exists() else 0

        return {
            "query_id": session_id,
            "filename": f"multi_table_{session_id[:10]}.parquet",
            "row_count": row_count,
            "columns": cols,
            "file_size_bytes": file_size,
            "detected_format": "PARQUET",
            "sql": sql,
            "tables": table_names,
            "relationships_count": len(state.relationships),
        }

    def export_joined_csv(self, session_id: str, output_path: Path, join_rel_id: Optional[str] = None, user_id: Optional[str] = None) -> Path:
        """Execute relational JOIN in DuckDB and export the full merged dataset as CSV."""
        state = self._load_state(session_id, user_id=user_id)
        con = self._build_duckdb_connection(session_id, user_id=state.user_id)
        if not state.tables:
            raise ValueError("No tables staged in modeling session.")

        if join_rel_id:
            rel = next((r for r in state.relationships if r.id == join_rel_id), None)
            if not rel:
                raise ValueError(f"Relationship '{join_rel_id}' not found.")
            query = (
                f'SELECT * FROM "{rel.from_table}" '
                f'JOIN "{rel.to_table}" '
                f'ON "{rel.from_table}"."{rel.from_column}" = "{rel.to_table}"."{rel.to_column}"'
            )
        elif state.relationships:
            # Build full chained multi-table join
            base_tbl = state.relationships[0].from_table
            joins = []
            joined_tables = {base_tbl}
            for r in state.relationships:
                if r.to_table not in joined_tables:
                    joins.append(f'LEFT JOIN "{r.to_table}" ON "{r.from_table}"."{r.from_column}" = "{r.to_table}"."{r.to_column}"')
                    joined_tables.add(r.to_table)
                elif r.from_table not in joined_tables:
                    joins.append(f'LEFT JOIN "{r.from_table}" ON "{r.from_table}"."{r.from_column}" = "{r.to_table}"."{r.to_column}"')
                    joined_tables.add(r.from_table)
            query = f'SELECT * FROM "{base_tbl}" {" ".join(joins)}'
        else:
            first_tbl = list(state.tables.keys())[0]
            query = f'SELECT * FROM "{first_tbl}"'

        output_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ({query}) TO '{output_path.as_posix()}' (HEADER, DELIMITER ',')")
        return output_path
        if not state.tables:
            raise ValueError("No tables staged in modeling session.")

        if join_rel_id:
            rel = next((r for r in state.relationships if r.id == join_rel_id), None)
            if not rel:
                raise ValueError(f"Relationship '{join_rel_id}' not found.")
            query = (
                f'SELECT * FROM "{rel.from_table}" '
                f'JOIN "{rel.to_table}" '
                f'ON "{rel.from_table}"."{rel.from_column}" = "{rel.to_table}"."{rel.to_column}"'
            )
        elif state.relationships:
            # Build full chained multi-table join
            base_tbl = state.relationships[0].from_table
            joins = []
            joined_tables = {base_tbl}
            for r in state.relationships:
                if r.to_table not in joined_tables:
                    joins.append(f'LEFT JOIN "{r.to_table}" ON "{r.from_table}"."{r.from_column}" = "{r.to_table}"."{r.to_column}"')
                    joined_tables.add(r.to_table)
                elif r.from_table not in joined_tables:
                    joins.append(f'LEFT JOIN "{r.from_table}" ON "{r.from_table}"."{r.from_column}" = "{r.to_table}"."{r.to_column}"')
                    joined_tables.add(r.from_table)
            query = f'SELECT * FROM "{base_tbl}" {" ".join(joins)}'
        else:
            first_tbl = list(state.tables.keys())[0]
            query = f'SELECT * FROM "{first_tbl}"'

        output_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ({query}) TO '{output_path.as_posix()}' (HEADER, DELIMITER ',')")
        return output_path
