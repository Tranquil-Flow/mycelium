"""Small backend-neutral value object for symmetric row-wise int8 weights."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Int8RowwiseWeight:
    """An output-row quantized matrix and one positive scale per row."""

    values: Any
    scales: Any

    @property
    def shape(self) -> Any:
        return self.values.shape

    @property
    def dtype(self) -> Any:
        return self.values.dtype


__all__ = ["Int8RowwiseWeight"]
