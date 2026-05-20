class ModelError(Exception):
    """Base exception for model-related errors."""


class ExtractionError(ModelError):
    """Raised when LLM extraction or embedding fails (bad JSON, schema mismatch, etc.).

    Carries an optional structured ``status_code`` so callers can branch on the
    underlying HTTP status (e.g. an embedding client raising HTTP 400 for a
    content rejection vs. 429/5xx for transient failures) instead of matching on
    the human-readable message, which interpolates server response bodies
    verbatim and is unsafe to discriminate on.
    """

    def __init__(self, *args: object, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


class PipelineValidationError(ModelError):
    """Raised when an LLM-generated pipeline fails safety validation."""
