"""Neutral intermediate representation (IR) compiled from a validated DataMorph Recipe."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from core.recipe_schema import Recipe

MacroKind = Literal[
    "trim_and_normalize",
    "parse_timestamp",
    "cast_type",
    "clip_outliers",
    "handle_nulls",
    "mask_pii",
    "dedupe",
]

IRStage = Literal["normalize", "filter", "impute", "mask", "dedupe"]


@dataclass(frozen=True)
class IRStep:
    """Single ordered transformation step inside a RecipeIR."""

    stage: IRStage
    macro: MacroKind
    column: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IRAssertion:
    """Data-quality assertion derived from recipe rules (used by dbt schema tests)."""

    kind: Literal["unique", "not_null", "accepted_range", "unique_combination"]
    columns: List[str]
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RecipeIR:
    """Ordered, target-neutral intermediate representation of a cleaning recipe."""

    version: int
    columns: List[str]
    has_full_column_list: bool
    steps: List[IRStep] = field(default_factory=list)
    assertions: List[IRAssertion] = field(default_factory=list)
    ir_warnings: List[str] = field(default_factory=list)

    def steps_for_stage(self, stage: IRStage) -> List[IRStep]:
        return [s for s in self.steps if s.stage == stage]


def _collect_ordered_columns(
    recipe: Recipe,
    profile: Optional[Dict[str, Any]],
) -> tuple[List[str], bool]:
    ordered: List[str] = []
    seen: set[str] = set()
    has_full = False

    if isinstance(profile, dict) and isinstance(profile.get("columns"), dict) and profile["columns"]:
        has_full = True
        for col in profile["columns"].keys():
            if col not in seen:
                seen.add(col)
                ordered.append(col)

    for col in recipe.columns.keys():
        if col not in seen:
            seen.add(col)
            ordered.append(col)

    if recipe.dedupe:
        for key in recipe.dedupe.keys:
            if key not in seen:
                seen.add(key)
                ordered.append(key)
        if recipe.dedupe.order_by and recipe.dedupe.order_by not in seen:
            seen.add(recipe.dedupe.order_by)
            ordered.append(recipe.dedupe.order_by)

    return ordered, has_full


def compile_recipe_to_ir(
    recipe: Recipe | Dict[str, Any],
    profile: Optional[Dict[str, Any]] = None,
) -> RecipeIR:
    """Compile a Recipe into a neutral IR preserving RecipeCompiler execution order."""
    validated = recipe if isinstance(recipe, Recipe) else Recipe.model_validate(recipe)
    columns, has_full = _collect_ordered_columns(validated, profile)
    steps: List[IRStep] = []
    assertions: List[IRAssertion] = []
    warnings: List[str] = []

    # Stage 1: normalize (per-column inline chain in RecipeCompiler: trim -> parse_ts -> cast -> clip)
    for col in columns:
        rule = validated.columns.get(col)
        if not rule:
            continue

        if rule.trim_and_normalize is not None:
            cfg = rule.trim_and_normalize
            steps.append(
                IRStep(
                    stage="normalize",
                    macro="trim_and_normalize",
                    column=col,
                    options={
                        "trim": cfg.trim,
                        "case": cfg.case,
                        "null_tokens": list(cfg.null_tokens),
                    },
                )
            )

        if rule.parse_timestamp is not None:
            cfg = rule.parse_timestamp
            steps.append(
                IRStep(
                    stage="normalize",
                    macro="parse_timestamp",
                    column=col,
                    options={
                        "formats": list(cfg.formats),
                        "source_tz": cfg.source_tz,
                    },
                )
            )
            if cfg.source_tz and cfg.source_tz.upper() != "UTC":
                warnings.append(
                    f"Column '{col}' specifies source_tz='{cfg.source_tz}', which DataMorph's "
                    "compiler does not currently convert during parse_timestamp."
                )

        if rule.cast_type is not None:
            cfg = rule.cast_type
            steps.append(
                IRStep(
                    stage="normalize",
                    macro="cast_type",
                    column=col,
                    options={"type": cfg.type},
                )
            )

        if rule.clip_outliers is not None:
            cfg = rule.clip_outliers
            steps.append(
                IRStep(
                    stage="normalize",
                    macro="clip_outliers",
                    column=col,
                    options={
                        "min": cfg.min,
                        "max": cfg.max,
                        "action": cfg.action,
                    },
                )
            )
            range_params: Dict[str, Any] = {}
            if cfg.min is not None:
                range_params["min_value"] = cfg.min
            if cfg.max is not None:
                range_params["max_value"] = cfg.max
            if range_params:
                assertions.append(
                    IRAssertion(
                        kind="accepted_range",
                        columns=[col],
                        params=range_params,
                    )
                )

    # Stage 2: filter (handle_nulls drop_row)
    for col in columns:
        rule = validated.columns.get(col)
        if rule and rule.handle_nulls and rule.handle_nulls.strategy == "drop_row":
            steps.append(
                IRStep(
                    stage="filter",
                    macro="handle_nulls",
                    column=col,
                    options={"strategy": "drop_row", "value": None},
                )
            )
            assertions.append(IRAssertion(kind="not_null", columns=[col]))

    # Stage 3: impute (handle_nulls fill_value / fill_median / fill_mode / flag)
    for col in columns:
        rule = validated.columns.get(col)
        if rule and rule.handle_nulls and rule.handle_nulls.strategy != "drop_row":
            cfg = rule.handle_nulls
            if cfg.strategy not in ("fill_value", "fill_median", "fill_mode", "flag"):
                raise ValueError(f"Unsupported handle_nulls strategy: {cfg.strategy}")
            steps.append(
                IRStep(
                    stage="impute",
                    macro="handle_nulls",
                    column=col,
                    options={"strategy": cfg.strategy, "value": cfg.value},
                )
            )
            if cfg.strategy in ("fill_value", "fill_median", "fill_mode"):
                assertions.append(IRAssertion(kind="not_null", columns=[col]))
            elif cfg.strategy == "flag":
                warnings.append(
                    f"handle_nulls strategy='flag' on column '{col}' is a no-op in DataMorph's compiler."
                )

    # Stage 4: mask (mask_pii)
    for col in columns:
        rule = validated.columns.get(col)
        if rule and rule.mask_pii is not None:
            cfg = rule.mask_pii
            steps.append(
                IRStep(
                    stage="mask",
                    macro="mask_pii",
                    column=col,
                    options={
                        "strategy": cfg.strategy,
                        "entity_type": cfg.entity_type,
                    },
                )
            )

    # Stage 5: dedupe
    if validated.dedupe is not None:
        cfg = validated.dedupe
        steps.append(
            IRStep(
                stage="dedupe",
                macro="dedupe",
                column=None,
                options={
                    "keys": list(cfg.keys),
                    "keep": cfg.keep,
                    "order_by": cfg.order_by,
                },
            )
        )
        if cfg.keep == "all":
            warnings.append(
                "Recipe dedupe.keep='all' still applies row_number() = 1 in DataMorph's DedupeMacro; "
                "exported code mirrors DataMorph's deduplication behavior."
            )
        if len(cfg.keys) == 1:
            assertions.append(IRAssertion(kind="unique", columns=list(cfg.keys)))
            assertions.append(IRAssertion(kind="not_null", columns=list(cfg.keys)))
        else:
            assertions.append(IRAssertion(kind="unique_combination", columns=list(cfg.keys)))
            for k in cfg.keys:
                assertions.append(IRAssertion(kind="not_null", columns=[k]))

    return RecipeIR(
        version=validated.version,
        columns=columns,
        has_full_column_list=has_full,
        steps=steps,
        assertions=assertions,
        ir_warnings=warnings,
    )
