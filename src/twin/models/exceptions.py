class ModelError(Exception):
    """Base exception for model-related errors."""


class ExtractionError(ModelError):
    """Raised when LLM extraction fails (bad JSON, schema mismatch, etc.)."""


class PipelineValidationError(ModelError):
    """Raised when an LLM-generated pipeline fails safety validation."""
