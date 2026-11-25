"""Standalone readable pandas script emitter."""

from __future__ import annotations

from typing import List, Set

from core.export.base import EmitterOutput
from core.export.headers import build_export_header, sanitize_identifier
from core.export.ir import IRStep, RecipeIR
from core.export.models import ExportOptions, ExportedFile

_HELPER_PARSE_TS = '''
def _parse_timestamps(series: pd.Series, formats: list[str]) -> pd.Series:
    """Try timestamp formats sequentially and return naive UTC-aligned datetimes."""
    cleaned = series.astype("string").str.strip()
    result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    for fmt in formats:
        parsed = pd.to_datetime(cleaned, format=fmt, errors="coerce")
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_convert("UTC").dt.tz_localize(None)
        result = result.fillna(parsed)
    return result
'''.strip("\n")

_HELPER_CAST_BOOL = '''
def _cast_boolean(series: pd.Series) -> pd.Series:
    """Cast common truthy/falsy string tokens to nullable pandas boolean."""
    norm = series.astype("string").str.strip().str.lower()
    mapping = {
        "true": True, "t": True, "1": True, "yes": True, "y": True,
        "false": False, "f": False, "0": False, "no": False, "n": False,
    }
    return norm.map(mapping).astype("boolean")
'''.strip("\n")

_HELPER_MASK_PII = '''
def _mask_pii_value(value: object, strategy: str, entity_type: str | None) -> object:
    """Mirror DataMorph DuckDB MaskPiiMacro semantics for a single non-null value."""
    if pd.isna(value):
        return pd.NA
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


class PandasEmitter:
    """Emits a standalone pandas cleaning script with clean(df) -> df."""

    name: str = "pandas"
    file_extension: str = ".py"

    def emit(self, ir: RecipeIR, options: ExportOptions) -> EmitterOutput:
        warnings: List[str] = list(ir.ir_warnings)
        seen_warnings: Set[str] = set(warnings)

        def _warn(msg: str) -> None:
            if msg not in seen_warnings:
                seen_warnings.add(msg)
                warnings.append(msg)

        used_helpers: Set[str] = set()
        body_lines: List[str] = ["    df = df.copy()"]

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
                used_helpers.add("parse_ts")
                formats = repr(step.options.get("formats", []))
                body_lines.append(f"    df[{col_repr}] = _parse_timestamps(df[{col_repr}], {formats})")
            elif step.macro == "cast_type" and col is not None:
                body_lines.extend(self._emit_cast(col_repr, step, used_helpers, _warn))
            elif step.macro == "clip_outliers" and col is not None:
                body_lines.extend(self._emit_clip(col_repr, step))
            elif step.macro == "handle_nulls" and col is not None:
                body_lines.extend(self._emit_nulls(col_repr, step, _warn))
            elif step.macro == "mask_pii" and col is not None:
                used_helpers.add("mask_pii")
                strat = repr(step.options.get("strategy", "partial_mask"))
                ent = repr(step.options.get("entity_type"))
                body_lines.append(
                    f"    df[{col_repr}] = df[{col_repr}].map(lambda v: _mask_pii_value(v, {strat}, {ent}))"
                )
            elif step.macro == "dedupe":
                body_lines.extend(self._emit_dedupe(step, _warn))
            else:
                raise ValueError(f"Unsupported macro in IR: {step.macro}")

        body_lines.append("")
        body_lines.append("    return df")

        imports = [
            "from __future__ import annotations",
            "",
            "import argparse",
        ]
        if "mask_pii" in used_helpers:
            imports.append("import hashlib")
            imports.append("import re")
        imports.append("from pathlib import Path")
        imports.append("")
        imports.append("import pandas as pd")

        helpers: List[str] = []
        if "parse_ts" in used_helpers:
            helpers.append(_HELPER_PARSE_TS)
        if "cast_bool" in used_helpers:
            helpers.append(_HELPER_CAST_BOOL)
        if "mask_pii" in used_helpers:
            helpers.append(_HELPER_MASK_PII)

        header = build_export_header(
            target="pandas",
            job_id=options.job_id,
            timestamp_iso=options.utc_timestamp_iso(),
            dataset_name=options.dataset_name,
            caveats=warnings,
            comment_prefix="# ",
        )

        sections = [header, "\n".join(imports)]
        if helpers:
            sections.append("\n\n".join(helpers))
        sections.append("def clean(df: pd.DataFrame) -> pd.DataFrame:\n" + "\n".join(body_lines))
        sections.append(self._emit_main_block())

        stem = sanitize_identifier(options.dataset_name, fallback="dataset")
        filename = f"clean_{stem}_pandas.py" if options.dataset_name else "clean_pandas.py"
        return EmitterOutput(
            files=[ExportedFile(name=filename, content="\n\n".join(sections) + "\n", language="python")],
            warnings=warnings,
        )

    @staticmethod
    def _emit_trim(col_repr: str, step: IRStep, warn) -> List[str]:
        trim = bool(step.options.get("trim", True))
        case = step.options.get("case")
        null_tokens = [str(t).lower() for t in (step.options.get("null_tokens") or [])]

        lines = [f"    _raw = df[{col_repr}].astype('string')"]
        if null_tokens:
            tokens_repr = repr(null_tokens)
            lines.append(f"    _is_null_token = _raw.str.strip().str.lower().isin({tokens_repr})")
        expr = "_raw.str.strip()" if trim else "_raw"
        if case == "lower":
            expr = f"{expr}.str.lower()"
        elif case == "upper":
            expr = f"{expr}.str.upper()"
        elif case == "title":
            expr = f"{expr}.str.title()"
            warn("Title-case in pandas (.str.title()) may differ from DataMorph on apostrophes and multi-word strings.")
        lines.append(f"    _norm = {expr}")
        if null_tokens:
            lines.append(f"    df[{col_repr}] = _norm.mask(_is_null_token, pd.NA)")
        else:
            lines.append(f"    df[{col_repr}] = _norm")
        return lines

    @staticmethod
    def _emit_cast(col_repr: str, step: IRStep, used_helpers: Set[str], warn) -> List[str]:
        target_type = step.options["type"]
        if target_type in ("integer", "bigint"):
            warn("pandas integer casts use nullable Int64; float inputs with decimals become NA unlike DuckDB rounding.")
            return [f"    df[{col_repr}] = pd.to_numeric(df[{col_repr}], errors='coerce').astype('Int64')"]
        if target_type == "double":
            return [f"    df[{col_repr}] = pd.to_numeric(df[{col_repr}], errors='coerce').astype('float64')"]
        if target_type == "boolean":
            used_helpers.add("cast_bool")
            return [f"    df[{col_repr}] = _cast_boolean(df[{col_repr}])"]
        if target_type == "varchar":
            return [f"    df[{col_repr}] = df[{col_repr}].astype('string')"]
        if target_type == "date":
            return [f"    df[{col_repr}] = pd.to_datetime(df[{col_repr}], errors='coerce').dt.date"]
        if target_type == "timestamp":
            return [f"    df[{col_repr}] = pd.to_datetime(df[{col_repr}], errors='coerce')"]
        raise ValueError(f"Unsupported cast_type target: {target_type}")

    @staticmethod
    def _emit_clip(col_repr: str, step: IRStep) -> List[str]:
        min_v = step.options.get("min")
        max_v = step.options.get("max")
        action = step.options.get("action", "null")
        lines = [f"    _num = pd.to_numeric(df[{col_repr}], errors='coerce')"]
        if action == "null":
            conds = []
            if min_v is not None:
                conds.append(f"(_num < {min_v})")
            if max_v is not None:
                conds.append(f"(_num > {max_v})")
            lines.append(f"    df[{col_repr}] = df[{col_repr}].mask({' | '.join(conds)}, pd.NA)")
            return lines
        if action == "clamp":
            lines.append(f"    df[{col_repr}] = _num.clip(lower={repr(min_v)}, upper={repr(max_v)})")
            return lines
        raise ValueError(f"Unsupported clip_outliers action: {action}")

    @staticmethod
    def _emit_nulls(col_repr: str, step: IRStep, warn) -> List[str]:
        strat = step.options["strategy"]
        if strat == "drop_row":
            return [f"    df = df.dropna(subset=[{col_repr}]).reset_index(drop=True)"]
        if strat == "fill_value":
            val = repr(step.options.get("value"))
            return [f"    df[{col_repr}] = df[{col_repr}].fillna({val})"]
        if strat == "fill_median":
            return [
                f"    _med = pd.to_numeric(df[{col_repr}], errors='coerce').median()",
                "    if pd.notna(_med):",
                f"        df[{col_repr}] = df[{col_repr}].fillna(_med)",
            ]
        if strat == "fill_mode":
            warn("fill_mode tie-breaking in pandas may differ from DuckDB MODE() when multiple values share the top frequency.")
            return [
                f"    _modes = df[{col_repr}].dropna().mode()",
                "    if not _modes.empty:",
                f"        df[{col_repr}] = df[{col_repr}].fillna(_modes.iloc[0])",
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
            ascending = keep != "latest"
            lines.append(
                f"    df = df.sort_values(by={repr(order_by)}, ascending={ascending}, na_position='last')"
            )
            lines.append(f"    df = df.drop_duplicates(subset={keys}, keep='first').reset_index(drop=True)")
        else:
            warn("Deduplication without order_by uses file row order in pandas; DuckDB QUALIFY order is non-deterministic.")
            pd_keep = "last" if keep == "latest" else "first"
            lines.append(f"    df = df.drop_duplicates(subset={keys}, keep={repr(pd_keep)}).reset_index(drop=True)")
        return lines

    @staticmethod
    def _emit_main_block() -> str:
        return '''if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run DataMorph cleaning recipe with pandas.")
    parser.add_argument("input_path", nargs="?", default="input.csv", help="Input CSV or Parquet file")
    parser.add_argument("output_path", nargs="?", default="cleaned.parquet", help="Output Parquet or CSV file")
    args = parser.parse_args()

    src = Path(args.input_path)
    dst = Path(args.output_path)
    if src.suffix.lower() == ".parquet":
        raw_df = pd.read_parquet(src).astype("string")
    else:
        raw_df = pd.read_csv(src, dtype=str, keep_default_na=False)

    cleaned_df = clean(raw_df)
    if dst.suffix.lower() == ".csv":
        cleaned_df.to_csv(dst, index=False)
    else:
        cleaned_df.to_parquet(dst, index=False)'''
