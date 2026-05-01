"""Shared types for the ``tree.data.web`` data layer."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    """A single organic SERP entry returned by Bright Data's SERP API."""

    rank: int = Field(
        ..., description="Position within the organic results, 1-indexed."
    )
    title: str
    url: str
    snippet: str = Field(
        default="", description="Description / page summary; may be empty."
    )
