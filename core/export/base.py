"""Base emitter protocol and output container for recipe code generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol

from core.export.ir import RecipeIR
from core.export.models import ExportOptions, ExportedFile


@dataclass
class EmitterOutput:
    """Files and parity warnings produced by a single target emitter."""

    files: List[ExportedFile] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class Emitter(Protocol):
    """Interface implemented by all recipe-to-code target emitters."""

    name: str
    file_extension: str

    def emit(self, ir: RecipeIR, options: ExportOptions) -> EmitterOutput:
        """Render the intermediate representation into target source files."""
        ...
