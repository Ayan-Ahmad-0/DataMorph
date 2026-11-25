"""CLI entrypoint for exporting a DataMorph recipe JSON file to SQL, dbt, pandas, or Polars."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from core.export.models import ExportCodeRequest
from core.export.service import export_recipe_as_code, package_export_zip


def _load_recipe_payload(raw: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[str]]:
    """Support either a raw Recipe JSON file or a persisted DataMorph job state.json."""
    if isinstance(raw.get("recipe"), dict) and ("metadata" in raw or "profile" in raw):
        meta = raw.get("metadata") or {}
        return raw["recipe"], raw.get("profile"), meta.get("filename")
    return raw, None, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.export",
        description="Export a DataMorph cleaning recipe JSON into SQL, dbt, pandas, or Polars code.",
    )
    parser.add_argument("--recipe", required=True, help="Path to recipe.json or job state.json")
    parser.add_argument(
        "--target",
        required=True,
        choices=["sql", "dbt", "pandas", "polars"],
        help="Target export format",
    )
    parser.add_argument(
        "--dialect",
        default="duckdb",
        choices=["duckdb", "snowflake", "bigquery", "postgres"],
        help="SQL dialect for sql/dbt targets (default: duckdb)",
    )
    parser.add_argument("--source-name", default=None, help="Optional source table name (e.g. raw.orders)")
    parser.add_argument("--job-id", default="cli", help="Job or recipe identifier for file headers")
    parser.add_argument("--dataset-name", default=None, help="Dataset name for file naming and headers")
    parser.add_argument("--no-tests", action="store_true", help="Omit dbt schema.yml tests")
    parser.add_argument("--output", "-o", default=None, help="Output file or directory path (default: stdout)")
    parser.add_argument("--zip", action="store_true", help="Write exported files as a .zip archive")

    args = parser.parse_args(argv)
    recipe_path = Path(args.recipe)
    if not recipe_path.exists():
        print(f"Error: recipe file not found: {recipe_path}", file=sys.stderr)
        return 1

    raw_json = json.loads(recipe_path.read_text(encoding="utf-8"))
    recipe_dict, profile_dict, inferred_name = _load_recipe_payload(raw_json)
    dataset_name = args.dataset_name or inferred_name

    request = ExportCodeRequest(
        target=args.target,
        dialect=args.dialect,
        include_tests=not args.no_tests,
        source_name=args.source_name,
    )
    result = export_recipe_as_code(
        recipe_dict,
        request,
        job_id=args.job_id,
        dataset_name=dataset_name,
        profile=profile_dict,
    )

    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    if args.zip:
        if not args.output:
            print("Error: --output is required when using --zip.", file=sys.stderr)
            return 2
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(package_export_zip(result))
        return 0

    if args.output:
        out_path = Path(args.output)
        if len(result.files) == 1 and out_path.suffix:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(result.files[0].content, encoding="utf-8")
        else:
            out_path.mkdir(parents=True, exist_ok=True)
            for f in result.files:
                (out_path / f.name).write_text(f.content, encoding="utf-8")
        return 0

    for idx, f in enumerate(result.files):
        if len(result.files) > 1:
            if idx > 0:
                print("")
            print(f"=== {f.name} ===")
        print(f.content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
