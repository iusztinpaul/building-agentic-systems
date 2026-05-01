"""Web data sources backed by Bright Data Web Unlocker."""

from tree.data.web.types import SearchResult
from tree.data.web.web_serp import search
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
    fetch_url,
)

__all__ = [
    "BrightDataConfigurationError",
    "BrightDataRequestError",
    "SearchResult",
    "fetch_url",
    "search",
]
