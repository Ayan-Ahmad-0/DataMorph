"""SQL snapshot, multi-dialect sqlglot transpilation, and dbt compile tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot

from core.export import ExportCodeRequest, export_recipe_as_code
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


def _sample_recipe() -> Recipe:
    return Recipe(
        version=1,
        dedupe=DedupeConfig(keys=["order_id"], keep="latest", order_by="order_ts"),
        columns={
            "customer_name": ColumnRule(
                trim_and_normalize=TrimAndNormalizeConfig(
                    trim=True, case="title", null_tokens=["N/A", "null", ""]
                )
            ),
            "order_ts": ColumnRule(
                parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"])
            ),
            "amount": ColumnRule(
                trim_and_normalize=TrimAndNormalizeConfig(trim=True, null_tokens=["N/A"]),
                cast_type=CastTypeConfig(type="double"),
                clip_outliers=ClipOutliersConfig(min=0.0, max=5000.0, action="clamp"),
                handle_nulls=HandleNullsConfig(strategy="fill_median"),
            ),
            "category": ColumnRule(
                handle_nulls=HandleNullsConfig(strategy="fill_mode")
            ),
            "email": ColumnRule(
                mask_pii=MaskPiiConfig(strategy="partial_mask", entity_type="email")
            ),
            "ssn": ColumnRule(
                mask_pii=MaskPiiConfig(strategy="sha256_hash", entity_type="ssn")
            ),
        },
    )


def _sample_profile() -> dict:
    return {
        "columns": {
            "order_id": {},
            "customer_name": {},
            "order_ts": {},
            "amount": {},
            "category": {},
            "email": {},
            "ssn": {},
        }
    }


def _strip_comments(sql: str) -> str:
    lines = [line for line in sql.splitlines() if not line.lstrip().startswith("--")]
    return "\n".join(lines).strip()


def test_duckdb_sql_snapshot_structure():
    resp = export_recipe_as_code(
        _sample_recipe(),
        ExportCodeRequest(target="sql", dialect="duckdb"),
        job_id="job_snap01",
        dataset_name="orders.csv",
        profile=_sample_profile(),
    )
    assert resp.target == "sql"
    assert resp.dialect == "duckdb"
    assert len(resp.files) == 1
    sql = resp.files[0].content

    assert "WITH staged AS (" in sql
    assert "FROM {{ source_table }}" in sql
    assert "normalized AS (" in sql
    assert "filled AS (" in sql
    assert "masked AS (" in sql
    assert "QUALIFY row_number() OVER (PARTITION BY \"order_id\" ORDER BY \"order_ts\" DESC) = 1" in sql
    assert "SELECT * REPLACE" not in sql


@pytest.mark.parametrize("dialect", ["duckdb", "snowflake", "bigquery", "postgres"])
def test_sql_transpiles_and_parses_across_all_dialects(dialect: str):
    resp = export_recipe_as_code(
        _sample_recipe(),
        ExportCodeRequest(target="sql", dialect=dialect, source_name="raw.orders"),  # type: ignore[arg-type]
        job_id="job_dial01",
        dataset_name="orders.csv",
        profile=_sample_profile(),
    )
    sql_body = _strip_comments(resp.files[0].content).rstrip(";")
    parsed = sqlglot.parse_one(sql_body, read=dialect)
    assert parsed is not None

    if dialect == "postgres":
        assert "QUALIFY" not in sql_body.upper()
        assert "ROW_NUMBER() OVER" in sql_body.upper()
        assert "PERCENTILE_CONT(0.5)" in sql_body.upper()
        assert "COUNT(*) DESC" in sql_body.upper()
        assert "LIMIT 1" in sql_body.upper()
        assert "SELECT * REPLACE" not in sql_body.upper()
        assert any("QUALIFY" in w for w in resp.warnings)
    elif dialect == "bigquery":
        assert "SAFE.PARSE_TIMESTAMP" in sql_body
        assert "SAFE_CAST" in sql_body
        assert "TO_HEX(SHA256" in sql_body
        assert "APPROX_QUANTILES" in sql_body
    elif dialect == "snowflake":
        assert "TRY_TO_TIMESTAMP" in sql_body


def test_dbt_emitter_generates_model_and_schema_structure():
    recipe = _sample_recipe()
    recipe.dedupe = DedupeConfig(keys=["order_id", "tenant_id"], keep="first")
    profile = _sample_profile()
    profile["columns"]["tenant_id"] = {}

    resp = export_recipe_as_code(
        recipe,
        ExportCodeRequest(target="dbt", dialect="duckdb", include_tests=True, source_name="raw.orders"),
        job_id="job_dbt01",
        dataset_name="orders.csv",
        profile=profile,
    )
    assert [f.name for f in resp.files] == ["cleaned_orders.sql", "schema.yml"]
    model_sql = resp.files[0].content
    schema_yml = resp.files[1].content

    assert "{{ config(materialized='table') }}" in model_sql
    assert "{{ source('raw', 'orders') }}" in model_sql
    assert "version: 2" in schema_yml
    assert "- name: cleaned_orders" in schema_yml
    assert "dbt_utils.unique_combination_of_columns" in schema_yml
    assert "not_null" in schema_yml
    assert "dbt_utils.accepted_range" in schema_yml

    resp_no_tests = export_recipe_as_code(
        _sample_recipe(),
        ExportCodeRequest(target="dbt", include_tests=False),
        job_id="job_dbt02",
        dataset_name="orders.csv",
        profile=_sample_profile(),
    )
    assert "columns:" not in resp_no_tests.files[1].content


def test_dbt_emitter_generates_model_and_valid_schema_yaml():
    yaml = pytest.importorskip("yaml")
    recipe = _sample_recipe()
    recipe.dedupe = DedupeConfig(keys=["order_id", "tenant_id"], keep="first")
    profile = _sample_profile()
    profile["columns"]["tenant_id"] = {}

    resp = export_recipe_as_code(
        recipe,
        ExportCodeRequest(target="dbt", dialect="duckdb", include_tests=True, source_name="raw.orders"),
        job_id="job_dbt01",
        dataset_name="orders.csv",
        profile=profile,
    )
    assert [f.name for f in resp.files] == ["cleaned_orders.sql", "schema.yml"]
    model_sql = resp.files[0].content
    schema_yml = resp.files[1].content

    assert "{{ config(materialized='table') }}" in model_sql
    assert "{{ source('raw', 'orders') }}" in model_sql

    parsed_yaml = yaml.safe_load(schema_yml)
    assert parsed_yaml["version"] == 2
    model_entry = parsed_yaml["models"][0]
    assert model_entry["name"] == "cleaned_orders"
    assert any("dbt_utils.unique_combination_of_columns" in t for t in model_entry["tests"])

    col_map = {c["name"]: c["tests"] for c in model_entry["columns"]}
    assert "not_null" in col_map["amount"]
    assert any(
        isinstance(t, dict) and "dbt_utils.accepted_range" in t
        for t in col_map["amount"]
    )


def test_dbt_emitter_respects_include_tests_false():
    yaml = pytest.importorskip("yaml")
    resp = export_recipe_as_code(
        _sample_recipe(),
        ExportCodeRequest(target="dbt", include_tests=False),
        job_id="job_dbt02",
        dataset_name="orders.csv",
        profile=_sample_profile(),
    )
    parsed_yaml = yaml.safe_load(resp.files[1].content)
    assert "columns" not in parsed_yaml["models"][0]


def test_dbt_compile_with_dbt_duckdb(tmp_path: Path):
    dbt_cli = pytest.importorskip("dbt.cli.main")
    pytest.importorskip("dbt.adapters.duckdb")

    # Create a minimal dbt project in tmp_path and compile our emitted model
    project_dir = tmp_path / "dbt_proj"
    models_dir = project_dir / "models"
    profiles_dir = tmp_path / "profiles"
    models_dir.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)

    (project_dir / "dbt_project.yml").write_text(
        "name: 'datamorph_test'\n"
        "version: '1.0.0'\n"
        "profile: 'datamorph_test'\n"
        "model-paths: ['models']\n",
        encoding="utf-8",
    )
    (profiles_dir / "profiles.yml").write_text(
        "datamorph_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: duckdb\n"
        f"      path: '{(tmp_path / 'test.duckdb').as_posix()}'\n",
        encoding="utf-8",
    )
    (models_dir / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: orders\n",
        encoding="utf-8",
    )

    # Emit dbt model without dbt_utils package requirement for standalone compile
    recipe = Recipe(
        dedupe=DedupeConfig(keys=["order_id"], keep="latest", order_by="order_ts"),
        columns={
            "customer_name": ColumnRule(
                trim_and_normalize=TrimAndNormalizeConfig(trim=True, case="lower")
            ),
            "order_ts": ColumnRule(
                parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%d %H:%M:%S"])
            ),
            "amount": ColumnRule(
                cast_type=CastTypeConfig(type="double"),
                handle_nulls=HandleNullsConfig(strategy="fill_value", value=0.0),
            ),
        },
    )
    resp = export_recipe_as_code(
        recipe,
        ExportCodeRequest(target="dbt", dialect="duckdb", include_tests=True, source_name="raw.orders"),
        job_id="job_dbt_compile",
        dataset_name="orders.csv",
        profile={"columns": {"order_id": {}, "customer_name": {}, "order_ts": {}, "amount": {}}},
    )
    for f in resp.files:
        (models_dir / f.name).write_text(f.content, encoding="utf-8")

    res = dbt_cli.dbtRunner().invoke(
        ["compile", "--project-dir", str(project_dir), "--profiles-dir", str(profiles_dir)]
    )
    assert res.success, f"dbt compile failed: {res.exception}"
    compiled_sql = (project_dir / "target" / "compiled" / "datamorph_test" / "models" / "cleaned_orders.sql").read_text(
        encoding="utf-8"
    )
    assert "WITH staged AS" in compiled_sql
    assert "QUALIFY row_number()" in compiled_sql
