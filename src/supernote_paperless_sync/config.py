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

    # Tag names (must already exist in Paperless)
    inbound_tag: str = Field(
        default="paperless-gpt-ocr-auto",
        description="Tag applied to notes ingested from Supernote",
    )
    outbound_tag: str = Field(
        default="send-to-supernote",
        description="Tag in Paperless that triggers export to Supernote",
    )
    outbound_subfolder: str = Field(
        default="Paperless",
        description="Subfolder inside Document/ where exported files are stored",
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
