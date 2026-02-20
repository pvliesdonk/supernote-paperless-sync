"""SQLite state database for tracking ingested notes and exported documents."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS ingested_notes (
    note_path  TEXT    NOT NULL PRIMARY KEY,
    mtime_ns   INTEGER NOT NULL,
    doc_id     INTEGER,
    ingested_at TEXT   NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS exported_docs (
    doc_id         INTEGER NOT NULL PRIMARY KEY,
    supernote_path TEXT    NOT NULL,
    checksum       TEXT,
    exported_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db(db_path: Path) -> None:
    """Create the state database and schema if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def _connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Inbound (notes → Paperless)
# ---------------------------------------------------------------------------


def get_ingested_mtime(db_path: Path, note_path: str) -> int | None:
    """Return the stored mtime_ns for a note, or None if not yet ingested."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT mtime_ns FROM ingested_notes WHERE note_path = ?",
            (note_path,),
        ).fetchone()
    return row["mtime_ns"] if row else None


def record_ingestion(
    db_path: Path, note_path: str, mtime_ns: int, doc_id: int | None
) -> None:
    """Upsert an ingested-note record."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ingested_notes (note_path, mtime_ns, doc_id, ingested_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(note_path) DO UPDATE SET
                mtime_ns   = excluded.mtime_ns,
                doc_id     = excluded.doc_id,
                ingested_at = excluded.ingested_at
            """,
            (note_path, mtime_ns, doc_id),
        )


# ---------------------------------------------------------------------------
# Outbound (Paperless → Supernote)
# ---------------------------------------------------------------------------


def get_exported_doc_ids(db_path: Path) -> set[int]:
    """Return the set of Paperless document IDs currently exported to Supernote."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT doc_id FROM exported_docs").fetchall()
    return {row["doc_id"] for row in rows}


def get_exported_path(db_path: Path, doc_id: int) -> str | None:
    """Return the Supernote file path for an exported document, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT supernote_path FROM exported_docs WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    return row["supernote_path"] if row else None


def record_export(
    db_path: Path, doc_id: int, supernote_path: str, checksum: str
) -> None:
    """Upsert an exported-document record."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO exported_docs (doc_id, supernote_path, checksum, exported_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(doc_id) DO UPDATE SET
                supernote_path = excluded.supernote_path,
                checksum       = excluded.checksum,
                exported_at    = excluded.exported_at
            """,
            (doc_id, supernote_path, checksum),
        )


def remove_export_record(db_path: Path, doc_id: int) -> None:
    """Delete the export record for a document."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM exported_docs WHERE doc_id = ?", (doc_id,))
