"""Configuration loaded from environment variables via Pydantic Settings."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration is read from environment variables (case-insensitive)."""

    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # Paperless connection
    paperless_url: str = Field(
        default="http://paperless-ngx:8000",
        description="Base URL of the Paperless-ngx API",
    )
    paperless_token: str = Field(description="Paperless-ngx API token")

    # Supernote paths (inside the container)
    supernote_note_dir: Path = Field(description="Path to Supernote Note/ directory")
    supernote_doc_dir: Path = Field(description="Path to Supernote Document/ directory")
    notelib_convert_dir: Path = Field(
        description="Path to notelib convert/ output directory"
    )

    # Metadata applied to ingested notes
    inbound_correspondent_override: str | None = Field(
        default=None,
        description=(
            "Override correspondent name for ingested notes. "
            "If blank, derived from the account directory in the note path."
        ),
    )
    inbound_document_type: str | None = Field(
        default=None,
        description="Document type name to assign to ingested notes (created if absent)",
    )

    # Tag names
    inbound_tag: str = Field(
        default="paperless-gpt-ocr-auto",
        description="Tag applied to notes ingested from Supernote",
    )
    inbound_completion_tag: str = Field(
        default="supernote-ingested",
        description="Tag applied after our own OCR/metadata pipeline completes",
    )
    superseded_tag: str = Field(
        default="superseded",
        description="Tag applied to old document version when a note is updated",
    )
    outbound_tag: str = Field(
        default="send-to-supernote",
        description="Tag in Paperless that triggers export to Supernote",
    )
    outbound_subfolder: str = Field(
        default="Paperless",
        description="Subfolder inside Document/ where exported files are stored",
    )

    # LLM (OpenAI-compatible) for OCR and metadata
    openai_base_url: str = Field(description="LiteLLM / OpenAI-compatible gateway URL")
    openai_api_key: str = Field(description="API key for the LLM gateway")
    vision_llm_model: str = Field(
        default="gpt-4o",
        description="Model used for vision-based handwriting OCR",
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        description="Model used for metadata suggestion (title, tags)",
    )

    # Behavior
    poll_interval: int = Field(
        default=60,
        ge=10,
        description="Seconds between outbound sync polls",
    )
    state_db: Path = Field(
        default=Path("/state/bridge.db"),
        description="Path to the SQLite state database",
    )
    log_level: str = Field(default="INFO", description="Logging level")
