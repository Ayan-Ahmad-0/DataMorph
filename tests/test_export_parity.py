"""Output-parity tests verifying exported code matches RecipeCompiler on synthetic datasets."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, List, Tuple

import duckdb
import pytest

from core.compiler import RecipeCompiler
from core.converter import Converter
from core.export import ExportCodeRequest, export_recipe_as_code
from core.profiler import Profiler
from core.recipe_schema import (
    CastTypeConfig,
    ClipOutliersConfig,
    ColumnRule,
    DedupeConfig,
    HandleNullsConfig,
    MaskPiiConfig,
    ParseTimestampConfig,
    Recipe,
    TrimAndNormalizeConfig,
)
from evaluation.datasets.generator import (
    generate_events,
    generate_reviews,
    generate_transactions,
)
from evaluation.injector import CorruptionInjector, InjectionConfig


def _fetch_sorted_table(parquet_path: Path, sort_cols: List[str]) -> Tuple[List[Tuple[str, str]], List[Tuple[Any, ...]]]:
    pq_sql = parquet_path.resolve().as_posix()
    conn = duckdb.connect(":memory:")
    try:
        schema = [(r[0], r[1]) for r in conn.execute(f"DESCRIBE SELECT * FROM read_parquet('{pq_sql}')").fetchall()]
        order_sql = ", ".join(f'"{c}" ASC NULLS LAST' for c in sort_cols)
        rows = conn.execute(f"SELECT * FROM read_parquet('{pq_sql}') ORDER BY {order_sql}").fetchall()
        return schema, rows
    finally:
        conn.close()


def _run_exported_sql_on_parquet(sql_text: str, staged_parquet: Path, out_parquet: Path) -> None:
    core_sql = sql_text.strip().rstrip(";")
    # Strip leading comment lines so wrapping in COPY (...) works cleanly
    body_lines = [line for line in core_sql.splitlines() if not line.lstrip().startswith("--")]
    body_sql = "\n".join(body_lines).strip()

    pq_in = staged_parquet.resolve().as_posix()
    pq_out = out_parquet.resolve().as_posix()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(f"CREATE OR REPLACE VIEW staged_raw AS SELECT * FROM read_parquet('{pq_in}');")
        conn.execute(f"COPY ({body_sql}) TO '{pq_out}' (FORMAT PARQUET);")
    finally:
        conn.close()


@pytest.mark.parametrize(
    "dataset_kind,generator_fn,recipe_builder,sort_cols",
    [
        (
            "events",
            generate_events,
            lambda: Recipe(
                dedupe=DedupeConfig(keys=["event_id"], keep="latest", order_by="timestamp"),
                columns={
                    "user_id": ColumnRule(trim_and_normalize=TrimAndNormalizeConfig(trim=True, null_tokens=["N/A", "null", ""])),
                    "event_type": ColumnRule(trim_and_normalize=TrimAndNormalizeConfig(trim=True, case="lower", null_tokens=["N/A", "null", ""])),
                    "timestamp": ColumnRule(parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"])),
                    "platform": ColumnRule(trim_and_normalize=TrimAndNormalizeConfig(trim=True, case="lower", null_tokens=["N/A", "null", ""])),
                    "session_duration_sec": ColumnRule(
                        trim_and_normalize=TrimAndNormalizeConfig(trim=True, null_tokens=["N/A", "null", ""]),
                        cast_type=CastTypeConfig(type="bigint"),
                        clip_outliers=ClipOutliersConfig(min=0.0, max=3600.0, action="null"),
                        handle_nulls=HandleNullsConfig(strategy="fill_median"),
                    ),
                },
            ),
            ["event_id"],
        ),
        (
            "transactions",
            generate_transactions,
            lambda: Recipe(
                dedupe=DedupeConfig(keys=["transaction_id"], keep="latest", order_by="created_at"),
                columns={
                    "customer_id": ColumnRule(trim_and_normalize=TrimAndNormalizeConfig(trim=True, null_tokens=["N/A", "null", ""])),
                    "amount": ColumnRule(
                        trim_and_normalize=TrimAndNormalizeConfig(trim=True, null_tokens=["N/A", "null", ""]),
                        cast_type=CastTypeConfig(type="double"),
                        clip_outliers=ClipOutliersConfig(min=0.0, max=5000.0, action="clamp"),
                        handle_nulls=HandleNullsConfig(strategy="fill_median"),
                    ),
                    "currency": ColumnRule(
                        trim_and_normalize=TrimAndNormalizeConfig(trim=True, case="upper", null_tokens=["N/A", "null", ""]),
                        handle_nulls=HandleNullsConfig(strategy="fill_mode"),
                    ),
                    "status": ColumnRule(
                        trim_and_normalize=TrimAndNormalizeConfig(trim=True, case="lower", null_tokens=["N/A", "null", ""]),
                        handle_nulls=HandleNullsConfig(strategy="drop_row"),
                    ),
                    "created_at": ColumnRule(parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"])),
                },
            ),
            ["transaction_id"],
        ),
        (
            "reviews",
            generate_reviews,
            lambda: Recipe(
                dedupe=DedupeConfig(keys=["review_id"], keep="first", order_by="review_date"),
                columns={
                    "rating": ColumnRule(
                        trim_and_normalize=TrimAndNormalizeConfig(trim=True, null_tokens=["N/A", "null", ""]),
                        cast_type=CastTypeConfig(type="integer"),
                        clip_outliers=ClipOutliersConfig(min=1.0, max=5.0, action="clamp"),
                        handle_nulls=HandleNullsConfig(strategy="fill_value", value=3),
                    ),
                    "helpful_votes": ColumnRule(
                        trim_and_normalize=TrimAndNormalizeConfig(trim=True, null_tokens=["N/A", "null", ""]),
                        cast_type=CastTypeConfig(type="bigint"),
                        clip_outliers=ClipOutliersConfig(min=0.0, action="null"),
                        handle_nulls=HandleNullsConfig(strategy="fill_value", value=0),
                    ),
                    "review_date": ColumnRule(parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%d", "%d/%m/%Y"])),
                },
            ),
            ["review_id"],
        ),
    ],
)
def test_duckdb_sql_parity_on_evaluation_datasets(
    tmp_path: Path,
    dataset_kind: str,
    generator_fn,
    recipe_builder,
    sort_cols: List[str],
):
    clean_csv = tmp_path / f"{dataset_kind}_clean.csv"
    dirty_csv = tmp_path / f"{dataset_kind}_dirty.csv"
    manifest_json = tmp_path / f"{dataset_kind}_manifest.json"
    staged_pq = tmp_path / f"{dataset_kind}_staged.parquet"
    compiler_out_pq = tmp_path / f"{dataset_kind}_compiler_out.parquet"
    exported_out_pq = tmp_path / f"{dataset_kind}_exported_out.parquet"

    generator_fn(clean_csv, num_rows=400, seed=42)
    CorruptionInjector(
        config=InjectionConfig(
            seed=42,
            null_token_rate=0.08,
            exact_duplicate_rate=0.05,
            whitespace_rate=0.10,
            negative_outlier_rate=0.05,
            mixed_date_rate=0.10,
        )
    ).inject_from_file(clean_csv, dirty_csv, manifest_json)

    Converter().convert(dirty_csv, staged_pq)
    profile = Profiler().profile_parquet(staged_pq).to_dict()
    recipe = recipe_builder()

    # Result A: DataMorph RecipeCompiler
    RecipeCompiler().execute_clean(recipe, staged_pq, compiler_out_pq)

    # Result B: Exported DuckDB SQL
    export_resp = export_recipe_as_code(
        recipe,
        ExportCodeRequest(target="sql", dialect="duckdb", source_name="staged_raw"),
        job_id=f"parity_{dataset_kind}",
        dataset_name=f"{dataset_kind}.csv",
        profile=profile,
    )
    _run_exported_sql_on_parquet(export_resp.files[0].content, staged_pq, exported_out_pq)

    schema_a, rows_a = _fetch_sorted_table(compiler_out_pq, sort_cols)
    schema_b, rows_b = _fetch_sorted_table(exported_out_pq, sort_cols)

    assert schema_a == schema_b
    assert len(rows_a) == len(rows_b)
    assert rows_a == rows_b


def test_duckdb_sql_parity_all_seven_macros_including_mask_pii(tmp_path: Path):
    staged_pq = tmp_path / "staged.parquet"
    compiler_out = tmp_path / "compiler.parquet"
    exported_out = tmp_path / "exported.parquet"

    conn = duckdb.connect(":memory:")
    conn.execute(
        f"""
        COPY (
            SELECT '1' AS id, '  Alice ' AS name, 'alice@example.com' AS email, '123-45-6789' AS ssn, '25.5' AS amount, '2025-01-01' AS dt, 'active' AS status
            UNION ALL
            SELECT '2' AS id, 'N/A' AS name, 'bob@test.org' AS email, '987-65-4321' AS ssn, '-10' AS amount, '02/01/2025' AS dt, 'active' AS status
            UNION ALL
            SELECT '2' AS id, 'N/A' AS name, 'bob@test.org' AS email, '987-65-4321' AS ssn, '-10' AS amount, '03/01/2025' AS dt, 'active' AS status
            UNION ALL
            SELECT '3' AS id, 'charlie' AS name, 'c@x.io' AS email, '111-22-3333' AS ssn, '100' AS amount, '2025-01-04' AS dt, 'N/A' AS status
        ) TO '{staged_pq.as_posix()}' (FORMAT PARQUET);
        """
    )
    conn.close()

    profile = Profiler().profile_parquet(staged_pq).to_dict()
    recipe = Recipe(
        dedupe=DedupeConfig(keys=["id"], keep="latest", order_by="dt"),
        columns={
            "name": ColumnRule(trim_and_normalize=TrimAndNormalizeConfig(trim=True, case="title", null_tokens=["N/A"])),
            "email": ColumnRule(mask_pii=MaskPiiConfig(strategy="partial_mask", entity_type="email")),
            "ssn": ColumnRule(mask_pii=MaskPiiConfig(strategy="sha256_hash", entity_type="ssn")),
            "amount": ColumnRule(
                cast_type=CastTypeConfig(type="double"),
                clip_outliers=ClipOutliersConfig(min=0.0, max=500.0, action="null"),
                handle_nulls=HandleNullsConfig(strategy="fill_median"),
            ),
            "dt": ColumnRule(parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%d", "%d/%m/%Y"])),
            "status": ColumnRule(
                trim_and_normalize=TrimAndNormalizeConfig(trim=True, null_tokens=["N/A"]),
                handle_nulls=HandleNullsConfig(strategy="drop_row"),
            ),
        },
    )

    RecipeCompiler().execute_clean(recipe, staged_pq, compiler_out)
    export_resp = export_recipe_as_code(
        recipe,
        ExportCodeRequest(target="sql", dialect="duckdb", source_name="staged_raw"),
        job_id="parity_all7",
        dataset_name="all7.csv",
        profile=profile,
    )
    _run_exported_sql_on_parquet(export_resp.files[0].content, staged_pq, exported_out)

    schema_a, rows_a = _fetch_sorted_table(compiler_out, ["id"])
    schema_b, rows_b = _fetch_sorted_table(exported_out, ["id"])
    assert schema_a == schema_b
    assert rows_a == rows_b


def test_optional_pandas_parity(tmp_path: Path):
    pd = pytest.importorskip("pandas")
    staged_pq = tmp_path / "staged.parquet"
    compiler_out = tmp_path / "compiler.parquet"

    conn = duckdb.connect(":memory:")
    conn.execute(
        f"""
        COPY (
            SELECT '1' AS id, '  alice ' AS name, 'a@ex.com' AS email, '10' AS val, '2025-01-01' AS dt
            UNION ALL
            SELECT '2' AS id, 'BOB' AS name, 'b@ex.com' AS email, '-5' AS val, '2025-01-02' AS dt
            UNION ALL
            SELECT '2' AS id, 'BOB' AS name, 'b@ex.com' AS email, '-5' AS val, '2025-01-03' AS dt
        ) TO '{staged_pq.as_posix()}' (FORMAT PARQUET);
        """
    )
    conn.close()

    recipe = Recipe(
        dedupe=DedupeConfig(keys=["id"], keep="latest", order_by="dt"),
        columns={
            "name": ColumnRule(trim_and_normalize=TrimAndNormalizeConfig(trim=True, case="lower")),
            "email": ColumnRule(mask_pii=MaskPiiConfig(strategy="sha256_hash", entity_type="email")),
            "val": ColumnRule(
                cast_type=CastTypeConfig(type="double"),
                clip_outliers=ClipOutliersConfig(min=0.0, max=100.0, action="clamp"),
            ),
            "dt": ColumnRule(parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%d"])),
        },
    )
    RecipeCompiler().execute_clean(recipe, staged_pq, compiler_out)
    export_resp = export_recipe_as_code(
        recipe,
        ExportCodeRequest(target="pandas"),
        job_id="parity_pd",
        dataset_name="pd_test.csv",
    )

    ns: dict[str, Any] = {}
    exec(export_resp.files[0].content, ns)
    raw_df = pd.read_parquet(staged_pq).astype("string")
    cleaned_df = ns["clean"](raw_df).sort_values("id").reset_index(drop=True)
    expected_df = pd.read_parquet(compiler_out).sort_values("id").reset_index(drop=True)

    assert len(cleaned_df) == len(expected_df)
    assert list(cleaned_df["name"]) == list(expected_df["name"])
    assert list(cleaned_df["email"]) == list(expected_df["email"])
    assert list(cleaned_df["val"]) == list(expected_df["val"])


def test_optional_polars_parity(tmp_path: Path):
    pl = pytest.importorskip("polars")
    assert importlib.util.find_spec("polars") is not None
    staged_pq = tmp_path / "staged.parquet"
    compiler_out = tmp_path / "compiler.parquet"

    conn = duckdb.connect(":memory:")
    conn.execute(
        f"""
        COPY (
            SELECT '1' AS id, '  alice ' AS name, 'a@ex.com' AS email, '10' AS val, '2025-01-01' AS dt
            UNION ALL
            SELECT '2' AS id, 'BOB' AS name, 'b@ex.com' AS email, '-5' AS val, '2025-01-02' AS dt
            UNION ALL
            SELECT '2' AS id, 'BOB' AS name, 'b@ex.com' AS email, '-5' AS val, '2025-01-03' AS dt
        ) TO '{staged_pq.as_posix()}' (FORMAT PARQUET);
        """
    )
    conn.close()

    recipe = Recipe(
        dedupe=DedupeConfig(keys=["id"], keep="latest", order_by="dt"),
        columns={
            "name": ColumnRule(trim_and_normalize=TrimAndNormalizeConfig(trim=True, case="lower")),
            "email": ColumnRule(mask_pii=MaskPiiConfig(strategy="sha256_hash", entity_type="email")),
            "val": ColumnRule(
                cast_type=CastTypeConfig(type="double"),
                clip_outliers=ClipOutliersConfig(min=0.0, max=100.0, action="clamp"),
            ),
            "dt": ColumnRule(parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%d"])),
        },
    )
    RecipeCompiler().execute_clean(recipe, staged_pq, compiler_out)
    export_resp = export_recipe_as_code(
        recipe,
        ExportCodeRequest(target="polars"),
        job_id="parity_pl",
        dataset_name="pl_test.csv",
    )

    ns: dict[str, Any] = {}
    exec(export_resp.files[0].content, ns)
    cleaned_df = ns["clean"](pl.scan_parquet(staged_pq)).collect().sort("id")
    expected_df = pl.read_parquet(compiler_out).sort("id")

    assert cleaned_df.height == expected_df.height
    assert cleaned_df["name"].to_list() == expected_df["name"].to_list()
    assert cleaned_df["email"].to_list() == expected_df["email"].to_list()
    assert cleaned_df["val"].to_list() == expected_df["val"].to_list()
