"""Output re-profiling, verification test execution, and before/after comparison."""

from __future__ import annotations
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import duckdb

from core.macros.base import quote_ident
from core.duckdb_utils import (
    configure_duckdb_connection,
    get_duckdb_memory_limit,
    get_duckdb_temp_directory,
    get_duckdb_threads,
)
from core.profiler import Profiler, DatasetProfile
from core.recipe_schema import Recipe

logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    test_name: str
    column_name: Optional[str]
    rule_type: str
    passed: bool
    details: str


@dataclass
class BeforeAfterComparison:
    rows_before: int
    rows_after: int
    rows_delta: int
    duplicates_before: int
    duplicates_after: int
    nulls_by_column: Dict[str, Dict[str, int]]
    negatives_by_column: Dict[str, Dict[str, int]]


@dataclass
class VerificationResult:
    status: str  # "PASS" or "DONE_WITH_WARNINGS"
    tests_passed: int
    tests_failed: int
    test_results: List[TestResult]
    comparison: BeforeAfterComparison
    output_profile: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "test_results": [asdict(t) for t in self.test_results],
            "comparison": asdict(self.comparison),
            "output_profile": self.output_profile,
        }


class Verifier:
    """Re-profiles output and executes verification assertions against the recipe."""

    def __init__(self, memory_limit: Optional[str] = None, threads: Optional[int] = None, temp_directory: Optional[str] = None) -> None:
        self.memory_limit = memory_limit or get_duckdb_memory_limit()
        self.threads = threads or get_duckdb_threads()
        self.temp_directory = temp_directory or get_duckdb_temp_directory()
        self.profiler = Profiler(memory_limit=self.memory_limit, threads=self.threads, temp_directory=self.temp_directory)

    def verify(
        self,
        cleaned_parquet_path: str | Path,
        before_profile: Dict[str, Any],
        recipe: Recipe,
        output_verification_json: Optional[str | Path] = None,
    ) -> VerificationResult:
        pq_path = Path(cleaned_parquet_path).resolve()
        if not pq_path.exists():
            raise FileNotFoundError(f"Cleaned file not found: {pq_path}")

        # 1. Compute lightweight output stats via a single consolidated DuckDB scan
        #    instead of calling the full profile_parquet() (which re-runs type inference,
        #    date probing, 9 null-token scans, etc. — all redundant on already-clean data).
        rows_before = before_profile.get("file", {}).get("rows", 0)
        dups_before = before_profile.get("duplicates", {}).get("exact_rows", 0)

        pq_sql = str(pq_path).replace("\\", "/")
        conn = duckdb.connect(":memory:")
        conn.execute("SET preserve_insertion_order = false;")
        if self.temp_directory:
            os.makedirs(self.temp_directory, exist_ok=True)
            conn.execute(f"SET temp_directory = '{self.temp_directory}';")
        conn.execute(f"SET memory_limit = '{self.memory_limit}';")
        conn.execute(f"SET threads = {self.threads};")

        try:
            view_name = "verified_clean_data"
            conn.execute(f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_parquet('{pq_sql}');")

            # --- Consolidated single-pass aggregation ---
            # Collect: row count, duplicate hash count, per-column null counts,
            # per-column negative counts, dedupe key duplicates, imputed-null counts,
            # and clip-violation counts — all in ONE scan of the parquet file.
            before_cols = before_profile.get("columns", {})
            all_before_col_names = list(before_cols.keys())

            # Build expressions for nulls and negatives per column
            agg_exprs = ["COUNT(*) AS _total_rows"]
            cols_quoted_hash = ", ".join(f'"{c}"' for c in all_before_col_names) if all_before_col_names else "1"
            agg_exprs.append(f"COUNT(*) - COUNT(DISTINCT hash({cols_quoted_hash})) AS _exact_dups")

            null_col_idxs: Dict[str, int] = {}
            neg_col_idxs: Dict[str, int] = {}

            for col in all_before_col_names:
                qcol = quote_ident(col)
                null_idx = len(agg_exprs)
                agg_exprs.append(f"SUM(CASE WHEN {qcol} IS NULL THEN 1 ELSE 0 END) AS _null_{null_idx}")
                null_col_idxs[col] = null_idx
                # Only compute negatives for columns that had them before cleaning
                if before_cols[col].get("negatives", 0) > 0:
                    neg_idx = len(agg_exprs)
                    agg_exprs.append(
                        f"SUM(CASE WHEN try_cast({qcol} AS DOUBLE) < 0 THEN 1 ELSE 0 END) AS _neg_{neg_idx}"
                    )
                    neg_col_idxs[col] = neg_idx

            # Verification assertion counts: dedupe-key duplicates, imputed-null remains,
            # clip-bound violations — all folded into the same aggregation.
            dedupe_key_idx: Optional[int] = None
            if recipe.dedupe:
                keys = recipe.dedupe.keys
                key_cols = ", ".join(quote_ident(k) for k in keys)
                dk_idx = len(agg_exprs)
                agg_exprs.append(
                    f"COUNT(*) - COUNT(DISTINCT ({key_cols})) AS _dedup_key_dups"
                )
                dedupe_key_idx = dk_idx

            imputed_null_idxs: Dict[str, int] = {}
            for col, rule in recipe.columns.items():
                if rule.handle_nulls and rule.handle_nulls.strategy in ("fill_value", "fill_median", "fill_mode"):
                    qcol = quote_ident(col)
                    idx = len(agg_exprs)
                    agg_exprs.append(
                        f"SUM(CASE WHEN {qcol} IS NULL THEN 1 ELSE 0 END) AS _imp_null_{idx}"
                    )
                    imputed_null_idxs[col] = idx

            clip_violation_idxs: Dict[str, int] = {}
            for col, rule in recipe.columns.items():
                if rule.clip_outliers:
                    cfg = rule.clip_outliers
                    qcol = quote_ident(col)
                    num_col = f"try_cast({qcol} AS DOUBLE)"
                    conds = []
                    if cfg.min is not None:
                        conds.append(f"{num_col} < {cfg.min}")
                    if cfg.max is not None:
                        conds.append(f"{num_col} > {cfg.max}")
                    if conds:
                        idx = len(agg_exprs)
                        agg_exprs.append(
                            f"SUM(CASE WHEN {' OR '.join(conds)} THEN 1 ELSE 0 END) AS _clip_{idx}"
                        )
                        clip_violation_idxs[col] = idx

            agg_sql = f"SELECT {', '.join(agg_exprs)} FROM {view_name};"
            agg_row = conn.execute(agg_sql).fetchone()

        except Exception:
            conn.close()
            raise

        rows_after = agg_row[0]
        dups_after = agg_row[1] or 0

        # 2. Compute Before/After deltas from consolidated scan
        nulls_comp: Dict[str, Dict[str, int]] = {}
        negs_comp: Dict[str, Dict[str, int]] = {}

        for col in all_before_col_names:
            before_meta = before_cols.get(col, {})
            nulls_comp[col] = {
                "before": before_meta.get("nulls", 0),
                "after": int(agg_row[null_col_idxs[col]] or 0),
            }
            if col in neg_col_idxs:
                negs_comp[col] = {
                    "before": before_meta.get("negatives", 0),
                    "after": int(agg_row[neg_col_idxs[col]] or 0),
                }

        comparison = BeforeAfterComparison(
            rows_before=rows_before,
            rows_after=rows_after,
            rows_delta=rows_after - rows_before,
            duplicates_before=dups_before,
            duplicates_after=dups_after,
            nulls_by_column=nulls_comp,
            negatives_by_column=negs_comp,
        )

        # Build a minimal after_dict for output_profile (file + columns nulls only)
        after_dict: Dict[str, Any] = {
            "file": {"rows": rows_after, "columns": len(all_before_col_names)},
            "duplicates": {"exact_rows": dups_after},
            "columns": {col: {"nulls": int(agg_row[null_col_idxs[col]] or 0)} for col in all_before_col_names},
        }

        # 3. Build Verification Test results from pre-computed consolidated scan values.
        #    No additional DuckDB queries are issued — all counts come from agg_row above.
        test_results: List[TestResult] = []

        try:
            # Test A: Deduplication Uniqueness Test
            if recipe.dedupe and dedupe_key_idx is not None:
                keys = recipe.dedupe.keys
                dup_count = int(agg_row[dedupe_key_idx] or 0)
                passed = (dup_count == 0)
                test_results.append(TestResult(
                    test_name="unique_key_test",
                    column_name=",".join(keys),
                    rule_type="dedupe",
                    passed=passed,
                    details=f"Dedupe key check: {dup_count} duplicate keys remaining in output." if not passed else "Unique constraint confirmed (0 duplicate keys).",
                ))

            # Test B: Not-Null assertions on imputed columns
            for col, idx in imputed_null_idxs.items():
                null_cnt = int(agg_row[idx] or 0)
                passed = (null_cnt == 0)
                test_results.append(TestResult(
                    test_name="not_null_test",
                    column_name=col,
                    rule_type="handle_nulls",
                    passed=passed,
                    details=f"Null check: {null_cnt} nulls remain." if not passed else "0 NULLs remain after imputation.",
                ))

            # Test C: Accepted range assertions on outlier-clipped columns
            for col, idx in clip_violation_idxs.items():
                violation_cnt = int(agg_row[idx] or 0)
                passed = (violation_cnt == 0)
                test_results.append(TestResult(
                    test_name="accepted_range_test",
                    column_name=col,
                    rule_type="clip_outliers",
                    passed=passed,
                    details=f"Bound check: {violation_cnt} values out of bounds." if not passed else "All values conform to bounding interval.",
                ))

            # Test D: Global reconciliation test
            reconciliation_passed = (rows_after <= rows_before)
            test_results.append(TestResult(
                test_name="row_reconciliation_test",
                column_name=None,
                rule_type="global",
                passed=reconciliation_passed,
                details=f"Reconciliation: Ingested {rows_before:,} rows, delivered {rows_after:,} rows (reconciled).",
            ))

        finally:
            conn.close()

        tests_passed = sum(1 for t in test_results if t.passed)
        tests_failed = sum(1 for t in test_results if not t.passed)
        status = "PASS" if tests_failed == 0 else "DONE_WITH_WARNINGS"

        result = VerificationResult(
            status=status,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            test_results=test_results,
            comparison=comparison,
            output_profile=after_dict,
        )

        if output_verification_json:
            out_v = Path(output_verification_json)
            out_v.parent.mkdir(parents=True, exist_ok=True)
            with open(out_v, "w", encoding="utf-8") as f:
                json.dump(result.to_dict(), f, indent=2)

        return result
