"""Standalone readable Polars lazy-expression script emitter."""

from __future__ import annotations

from typing import List, Set

from core.export.base import EmitterOutput
from core.export.headers import build_export_header, sanitize_identifier
from core.export.ir import IRStep, RecipeIR
from core.export.models import ExportOptions, ExportedFile

_HELPER_MASK_PII_POLARS = '''
def _mask_pii_value(value: object, strategy: str, entity_type: str | None) -> str | None:
    """Mirror DataMorph DuckDB MaskPiiMacro semantics for Polars map_elements."""
    if value is None:
        return None
    text = str(value)
    ent = (entity_type or "pii").lower()
    if strategy == "sha256_hash":
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if strategy == "redact":
        return f"[REDACTED_{ent.upper()}]"
    if ent == "email":
        return re.sub(r"^(.)(.*)(@.*)$", r"\\1***\\3", text) if "@" in text else "***@***"
    if ent in ("credit_card", "card"):
        digits = re.sub(r"[^0-9]", "", text)
        return f"****-****-****-{digits[-4:]}"
    if ent in ("ssn", "social_security"):
        digits = re.sub(r"[^0-9]", "", text)
        return f"***-**-{digits[-4:]}"
    if ent == "phone":
        digits = re.sub(r"[^0-9]", "", text)
        return f"(***) ***-{digits[-4:]}"
    if ent in ("ip", "ip_address"):
        if re.match(r"^[^.]*\\.[^.]*\\.[^.]*\\.[^.]*$", text):
            return re.sub(r"^([0-9]+\\.[0-9]+)\\..*$", r"\\1.*.*", text)
        return "[MASKED_IP]"
    return f"****{text[-4:]}" if len(text) > 4 else "****"
'''.strip("\n")


class PolarsEmitter:
    """Emits a standalone Polars lazy pipeline script with clean(lf) -> LazyFrame."""

    name: str = "polars"
    file_extension: str = ".py"

    def emit(self, ir: RecipeIR, options: ExportOptions) -> EmitterOutput:
        warnings: List[str] = list(ir.ir_warnings)
        seen_warnings: Set[str] = set(warnings)

        def _warn(msg: str) -> None:
            if msg not in seen_warnings:
                seen_warnings.add(msg)
                warnings.append(msg)

        needs_mask_helper = any(s.macro == "mask_pii" for s in ir.steps)
        body_lines: List[str] = []

        for idx, step in enumerate(ir.steps, start=1):
            col = step.column
            col_repr = repr(col) if col is not None else ""
            body_lines.append("")
            body_lines.append(
                f"    # Step {idx}: {step.macro}" + (f" ({col})" if col is not None else "")
            )

            if step.macro == "trim_and_normalize" and col is not None:
                body_lines.extend(self._emit_trim(col_repr, step, _warn))
            elif step.macro == "parse_timestamp" and col is not None:
                body_lines.extend(self._emit_parse_timestamp(col_repr, step))
            elif step.macro == "cast_type" and col is not None:
                body_lines.extend(self._emit_cast(col_repr, step, _warn))
            elif step.macro == "clip_outliers" and col is not None:
                body_lines.extend(self._emit_clip(col_repr, step))
            elif step.macro == "handle_nulls" and col is not None:
                body_lines.extend(self._emit_nulls(col_repr, step, _warn))
            elif step.macro == "mask_pii" and col is not None:
                strat = repr(step.options.get("strategy", "partial_mask"))
                ent = repr(step.options.get("entity_type"))
                body_lines.append(
                    f"    lf = lf.with_columns(\n"
                    f"        pl.col({col_repr}).map_elements(\n"
                    f"            lambda v: _mask_pii_value(v, {strat}, {ent}),\n"
                    f"            return_dtype=pl.String,\n"
                    f"        ).alias({col_repr})\n"
                    f"    )"
                )
            elif step.macro == "dedupe":
                body_lines.extend(self._emit_dedupe(step, _warn))
            else:
                raise ValueError(f"Unsupported macro in IR: {step.macro}")

        if not body_lines:
            body_lines.append("    pass")
        body_lines.append("")
        body_lines.append("    return lf")

        imports = [
            "from __future__ import annotations",
            "",
            "import argparse",
        ]
        if needs_mask_helper:
            imports.append("import hashlib")
            imports.append("import re")
        imports.append("from pathlib import Path")
        imports.append("")
        imports.append("import polars as pl")

        header = build_export_header(
            target="polars",
            job_id=options.job_id,
            timestamp_iso=options.utc_timestamp_iso(),
            dataset_name=options.dataset_name,
            caveats=warnings,
            comment_prefix="# ",
        )

        sections = [header, "\n".join(imports)]
        if needs_mask_helper:
            sections.append(_HELPER_MASK_PII_POLARS)
        sections.append("def clean(lf: pl.LazyFrame) -> pl.LazyFrame:\n" + "\n".join(body_lines).lstrip("\n"))
        sections.append(self._emit_main_block())

        stem = sanitize_identifier(options.dataset_name, fallback="dataset")
        filename = f"clean_{stem}_polars.py" if options.dataset_name else "clean_polars.py"
        return EmitterOutput(
            files=[ExportedFile(name=filename, content="\n\n".join(sections) + "\n", language="python")],
            warnings=warnings,
        )

    @staticmethod
    def _emit_trim(col_repr: str, step: IRStep, warn) -> List[str]:
        trim = bool(step.options.get("trim", True))
        case = step.options.get("case")
        null_tokens = [str(t).lower() for t in (step.options.get("null_tokens") or [])]

        expr = f"pl.col({col_repr}).cast(pl.String)"
        if trim:
            expr += ".str.strip_chars()"
        if case == "lower":
            expr += ".str.to_lowercase()"
        elif case == "upper":
            expr += ".str.to_uppercase()"
        elif case == "title":
            expr += ".str.to_titlecase()"
            warn("Title-case in Polars (.str.to_titlecase()) may differ from DataMorph on apostrophes and multi-word strings.")

        if null_tokens:
            tokens_repr = repr(null_tokens)
            cond = f"pl.col({col_repr}).cast(pl.String).str.strip_chars().str.to_lowercase().is_in({tokens_repr})"
            full_expr = f"pl.when({cond}).then(None).otherwise({expr})"
        else:
            full_expr = expr

        return [f"    lf = lf.with_columns({full_expr}.alias({col_repr}))"]

    @staticmethod
    def _emit_parse_timestamp(col_repr: str, step: IRStep) -> List[str]:
        formats = step.options.get("formats", [])
        calls = []
        for fmt in formats:
            call = f"pl.col({col_repr}).cast(pl.String).str.strip_chars().str.to_datetime({repr(fmt)}, strict=False)"
            if "%z" in fmt or fmt.endswith("Z"):
                call += ".dt.replace_time_zone(None)"
            calls.append(call)
        coalesce_expr = f"pl.coalesce([{', '.join(calls)}])" if len(calls) > 1 else calls[0]
        return [f"    lf = lf.with_columns({coalesce_expr}.alias({col_repr}))"]

    @staticmethod
    def _emit_cast(col_repr: str, step: IRStep, warn) -> List[str]:
        target_type = step.options["type"]
        if target_type == "integer":
            return [f"    lf = lf.with_columns(pl.col({col_repr}).cast(pl.Int32, strict=False).alias({col_repr}))"]
        if target_type == "bigint":
            return [f"    lf = lf.with_columns(pl.col({col_repr}).cast(pl.Int64, strict=False).alias({col_repr}))"]
        if target_type == "double":
            return [f"    lf = lf.with_columns(pl.col({col_repr}).cast(pl.Float64, strict=False).alias({col_repr}))"]
        if target_type == "boolean":
            norm = f"pl.col({col_repr}).cast(pl.String).str.strip_chars().str.to_lowercase()"
            expr = (
                f"pl.when({norm}.is_in(['true', 't', '1', 'yes', 'y'])).then(True)"
                f".when({norm}.is_in(['false', 'f', '0', 'no', 'n'])).then(False)"
                f".otherwise(None)"
            )
            return [f"    lf = lf.with_columns({expr}.alias({col_repr}))"]
        if target_type == "varchar":
            return [f"    lf = lf.with_columns(pl.col({col_repr}).cast(pl.String).alias({col_repr}))"]
        if target_type == "date":
            return [f"    lf = lf.with_columns(pl.col({col_repr}).cast(pl.Date, strict=False).alias({col_repr}))"]
        if target_type == "timestamp":
            warn("Polars .cast(pl.Datetime, strict=False) expects ISO-like strings when parse_timestamp is not used.")
            return [f"    lf = lf.with_columns(pl.col({col_repr}).cast(pl.Datetime, strict=False).alias({col_repr}))"]
        raise ValueError(f"Unsupported cast_type target: {target_type}")

    @staticmethod
    def _emit_clip(col_repr: str, step: IRStep) -> List[str]:
        min_v = step.options.get("min")
        max_v = step.options.get("max")
        action = step.options.get("action", "null")
        num_expr = f"pl.col({col_repr}).cast(pl.Float64, strict=False)"
        if action == "null":
            conds = []
            if min_v is not None:
                conds.append(f"({num_expr} < {min_v})")
            if max_v is not None:
                conds.append(f"({num_expr} > {max_v})")
            return [
                f"    lf = lf.with_columns(pl.when({' | '.join(conds)}).then(None).otherwise(pl.col({col_repr})).alias({col_repr}))"
            ]
        if action == "clamp":
            if min_v is not None and max_v is not None:
                return [
                    f"    lf = lf.with_columns({num_expr}.clip(lower_bound={min_v}, upper_bound={max_v}).alias({col_repr}))"
                ]
            if min_v is not None:
                return [
                    f"    lf = lf.with_columns({num_expr}.clip(lower_bound={min_v}).alias({col_repr}))"
                ]
            return [
                f"    lf = lf.with_columns({num_expr}.clip(upper_bound={max_v}).alias({col_repr}))"
            ]
        raise ValueError(f"Unsupported clip_outliers action: {action}")

    @staticmethod
    def _emit_nulls(col_repr: str, step: IRStep, warn) -> List[str]:
        strat = step.options["strategy"]
        if strat == "drop_row":
            return [f"    lf = lf.filter(pl.col({col_repr}).is_not_null())"]
        if strat == "fill_value":
            val = repr(step.options.get("value"))
            return [f"    lf = lf.with_columns(pl.col({col_repr}).fill_null({val}).alias({col_repr}))"]
        if strat == "fill_median":
            return [f"    lf = lf.with_columns(pl.col({col_repr}).fill_null(pl.col({col_repr}).median()).alias({col_repr}))"]
        if strat == "fill_mode":
            warn("fill_mode tie-breaking in Polars may differ from DuckDB MODE() when multiple values share the top frequency.")
            return [
                f"    lf = lf.with_columns(pl.col({col_repr}).fill_null(pl.col({col_repr}).drop_nulls().mode().first()).alias({col_repr}))"
            ]
        if strat == "flag":
            return ["    # handle_nulls strategy='flag' leaves the column unchanged (matching DataMorph)."]
        raise ValueError(f"Unsupported handle_nulls strategy: {strat}")

    @staticmethod
    def _emit_dedupe(step: IRStep, warn) -> List[str]:
        keys = repr(step.options["keys"])
        keep = step.options.get("keep", "latest")
        order_by = step.options.get("order_by")
        lines: List[str] = []
        if order_by:
            descending = keep == "latest"
            lines.append(
                f"    lf = lf.sort({repr(order_by)}, descending={descending}, nulls_last=True)"
            )
            lines.append(f"    lf = lf.unique(subset={keys}, keep='first', maintain_order=True)")
        else:
            warn("Deduplication without order_by uses file row order in Polars; DuckDB QUALIFY order is non-deterministic.")
            pl_keep = "last" if keep == "latest" else "first"
            lines.append(f"    lf = lf.unique(subset={keys}, keep={repr(pl_keep)}, maintain_order=True)")
        return lines

    @staticmethod
    def _emit_main_block() -> str:
        return '''if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run DataMorph cleaning recipe with Polars.")
    parser.add_argument("input_path", nargs="?", default="input.csv", help="Input CSV or Parquet file")
    parser.add_argument("output_path", nargs="?", default="cleaned.parquet", help="Output Parquet or CSV file")
    args = parser.parse_args()

    src = Path(args.input_path)
    dst = Path(args.output_path)
    if src.suffix.lower() == ".parquet":
        raw_lf = pl.scan_parquet(src)
    else:
        raw_lf = pl.scan_csv(src, infer_schema_length=0)

    cleaned_lf = clean(raw_lf)
    if dst.suffix.lower() == ".csv":
        cleaned_lf.collect().write_csv(dst)
    else:
        cleaned_lf.sink_parquet(dst)'''
