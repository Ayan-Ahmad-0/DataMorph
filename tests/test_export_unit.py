"""Unit, validation, security, packaging, and CLI tests for core.export."""

from __future__ import annotations

import ast
import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.export import (
    ExportCodeRequest,
    ExportCodeResponse,
    export_recipe_as_code,
    package_export_zip,
)
from core.export.__main__ import main as export_cli_main
from core.export.ir import compile_recipe_to_ir
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


def _full_seven_macro_recipe() -> Recipe:
    return Recipe(
        version=1,
        dedupe=DedupeConfig(keys=["order_id"], keep="latest", order_by="order_ts"),
        columns={
            "customer name": ColumnRule(
                trim_and_normalize=TrimAndNormalizeConfig(
                    trim=True, case="lower", null_tokens=["N/A", "null", ""]
                )
            ),
            "order_ts": ColumnRule(
                parse_timestamp=ParseTimestampConfig(
                    formats=["%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"],
                    source_tz="UTC",
                )
            ),
            "amount": ColumnRule(
                trim_and_normalize=TrimAndNormalizeConfig(trim=True, null_tokens=["N/A"]),
                cast_type=CastTypeConfig(type="double"),
                clip_outliers=ClipOutliersConfig(min=0.0, max=10000.0, action="clamp"),
                handle_nulls=HandleNullsConfig(strategy="fill_median"),
            ),
            "status": ColumnRule(
                handle_nulls=HandleNullsConfig(strategy="drop_row")
            ),
            "email": ColumnRule(
                mask_pii=MaskPiiConfig(strategy="partial_mask", entity_type="email")
            ),
        },
    )


def test_request_validation_forbids_extra_and_unsafe_source():
    req = ExportCodeRequest(target="sql", dialect="snowflake", source_name="raw.orders_2025")
    assert req.source_name == "raw.orders_2025"

    with pytest.raises(ValidationError):
        ExportCodeRequest(target="sql", unknown_field="nope")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        ExportCodeRequest(target="pyspark")  # type: ignore[arg-type]

    for bad_source in [
        "orders; DROP TABLE users",
        "orders\nSELECT 1",
        "orders -- comment",
        "../secret_table",
        ".leading_dot",
        "trailing_dot.",
        'bad"quote',
    ]:
        with pytest.raises(ValidationError):
            ExportCodeRequest(target="sql", source_name=bad_source)


def test_ir_orders_all_seven_macros_and_assertions():
    recipe = _full_seven_macro_recipe()
    profile = {
        "columns": {
            "order_id": {},
            "customer name": {},
            "order_ts": {},
            "amount": {},
            "status": {},
            "email": {},
        }
    }
    ir = compile_recipe_to_ir(recipe, profile=profile)

    assert ir.has_full_column_list is True
    assert [s.stage for s in ir.steps] == [
        "normalize",  # customer name trim
        "normalize",  # order_ts parse
        "normalize",  # amount trim
        "normalize",  # amount cast
        "normalize",  # amount clip
        "filter",     # status drop_row
        "impute",     # amount fill_median
        "mask",       # email mask_pii
        "dedupe",     # order_id dedupe
    ]
    assert [s.macro for s in ir.steps] == [
        "trim_and_normalize",
        "parse_timestamp",
        "trim_and_normalize",
        "cast_type",
        "clip_outliers",
        "handle_nulls",
        "handle_nulls",
        "mask_pii",
        "dedupe",
    ]
    assertion_kinds = {(a.kind, tuple(a.columns)) for a in ir.assertions}
    assert ("unique", ("order_id",)) in assertion_kinds
    assert ("not_null", ("order_id",)) in assertion_kinds
    assert ("not_null", ("status",)) in assertion_kinds
    assert ("not_null", ("amount",)) in assertion_kinds
    assert ("accepted_range", ("amount",)) in assertion_kinds


def test_ir_warnings_for_flag_non_utc_and_keep_all():
    recipe = Recipe(
        dedupe=DedupeConfig(keys=["id"], keep="all"),
        columns={
            "ts": ColumnRule(
                parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%d"], source_tz="America/New_York")
            ),
            "note": ColumnRule(handle_nulls=HandleNullsConfig(strategy="flag")),
        },
    )
    ir = compile_recipe_to_ir(recipe)
    joined = "\n".join(ir.ir_warnings)
    assert "source_tz='America/New_York'" in joined
    assert "strategy='flag'" in joined
    assert "keep='all'" in joined


def test_headers_strip_local_paths_and_include_metadata():
    recipe = _full_seven_macro_recipe()
    for target in ("sql", "dbt", "pandas", "polars"):
        resp = export_recipe_as_code(
            recipe,
            ExportCodeRequest(target=target),  # type: ignore[arg-type]
            job_id="job_1234abcd",
            dataset_name=r"C:\Users\secret_user\private_dir\orders.csv",
            extra_warnings=["Custom caveat"],
        )
        assert isinstance(resp, ExportCodeResponse)
        assert len(resp.files) >= 1
        assert "Custom caveat" in resp.warnings
        for f in resp.files:
            assert "secret_user" not in f.content
            assert "private_dir" not in f.content
            assert "C:\\Users" not in f.content
            assert "job_1234abcd" in f.content
            assert "orders.csv" in f.content or "orders" in f.content


def test_python_targets_generate_valid_ast_for_all_macros():
    recipe = Recipe(
        dedupe=DedupeConfig(keys=["id", "tenant id"], keep="first", order_by="ts"),
        columns={
            "name": ColumnRule(
                trim_and_normalize=TrimAndNormalizeConfig(trim=True, case="title", null_tokens=["N/A"])
            ),
            "ts": ColumnRule(
                parse_timestamp=ParseTimestampConfig(formats=["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"])
            ),
            "flag": ColumnRule(cast_type=CastTypeConfig(type="boolean")),
            "score": ColumnRule(
                cast_type=CastTypeConfig(type="integer"),
                clip_outliers=ClipOutliersConfig(min=0, max=100, action="null"),
                handle_nulls=HandleNullsConfig(strategy="fill_mode"),
            ),
            "ssn": ColumnRule(mask_pii=MaskPiiConfig(strategy="sha256_hash", entity_type="ssn")),
            "phone": ColumnRule(mask_pii=MaskPiiConfig(strategy="redact", entity_type="phone")),
            "card": ColumnRule(mask_pii=MaskPiiConfig(strategy="partial_mask", entity_type="credit_card")),
        },
    )
    for target in ("pandas", "polars"):
        resp = export_recipe_as_code(
            recipe,
            ExportCodeRequest(target=target),  # type: ignore[arg-type]
            job_id="job_py01",
            dataset_name="events.csv",
        )
        code = resp.files[0].content
        ast.parse(code)
        compile(code, resp.files[0].name, "exec")
        assert "def clean(" in code
        assert any("Title-case" in w for w in resp.warnings)
        assert any("fill_mode" in w for w in resp.warnings)


def test_zip_packaging_and_cli_roundtrip(tmp_path: Path):
    recipe = _full_seven_macro_recipe()
    recipe_file = tmp_path / "recipe.json"
    recipe_file.write_text(json.dumps(recipe.model_dump()), encoding="utf-8")

    # 1. ZIP via service
    resp = export_recipe_as_code(
        recipe,
        ExportCodeRequest(target="dbt", dialect="duckdb", source_name="raw.orders"),
        job_id="job_zip1",
        dataset_name="orders.csv",
    )
    zip_bytes = package_export_zip(resp)
    with zipfile.ZipFile(BytesIO(zip_bytes), "r") as zf:
        names = set(zf.namelist())
        assert names == {"cleaned_orders.sql", "schema.yml"}

    # 2. CLI directory output
    out_dir = tmp_path / "dbt_out"
    rc = export_cli_main(
        ["--recipe", str(recipe_file), "--target", "dbt", "--job-id", "job_cli", "--output", str(out_dir)]
    )
    assert rc == 0
    assert (out_dir / "cleaned_dataset.sql").exists()
    assert (out_dir / "schema.yml").exists()

    # 3. CLI ZIP output using state.json envelope
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "metadata": {"filename": "customer_orders.csv"},
                "profile": {"columns": {c: {} for c in ["order_id", "customer name", "order_ts", "amount", "status", "email"]}},
                "recipe": recipe.model_dump(),
            }
        ),
        encoding="utf-8",
    )
    zip_out = tmp_path / "export.zip"
    rc_zip = export_cli_main(
        ["--recipe", str(state_file), "--target", "sql", "--dialect", "postgres", "--zip", "-o", str(zip_out)]
    )
    assert rc_zip == 0
    with zipfile.ZipFile(zip_out, "r") as zf:
        assert "clean_customer_orders.sql" in zf.namelist()
