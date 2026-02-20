# supernote-paperless-sync

Bidirectional sync between [Supernote](https://supernote.com/) private cloud
and [Paperless-ngx](https://docs.paperless-ngx.com/).

## Features

**Inbound** — `.note` files → Paperless:
- Watches the Supernote `Note/` directory for new and modified files
- Uses pre-converted PDFs from `notelib` when available (hybrid approach)
- Falls back to [`supernotelib`](https://github.com/jya-dev/supernote-tool) for conversion
- Tags ingested documents for LLM-based OCR (`paperless-gpt-ocr-auto`)
- Re-ingests modified notes on change (tracks mtime)
- Startup scan catches notes synced while the service was offline

**Outbound** — Paperless → Supernote:
- Polls Paperless for documents tagged `send-to-supernote`
- Downloads originals and writes them to `Document/Paperless/` on the device
- Files are named from the Paperless document title
- **Removing the tag deletes the file from Supernote** — Paperless is source of truth

## Architecture

```
Supernote Tablet ←→ Supernote Private Cloud ←→ supernote-bridge ←→ Paperless-ngx
                          .note files                    ↓
                          Document/Paperless/       paperless-gpt (GPT-4o OCR)
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PAPERLESS_URL` | `http://paperless-ngx:8000` | Paperless API base URL |
| `PAPERLESS_TOKEN` | *(required)* | Paperless API token |
| `SUPERNOTE_NOTE_DIR` | *(required)* | Path to Supernote `Note/` directory |
| `SUPERNOTE_DOC_DIR` | *(required)* | Path to Supernote `Document/` directory |
| `NOTELIB_CONVERT_DIR` | *(required)* | Path to notelib `convert/` directory |
| `INBOUND_TAG` | `paperless-gpt-ocr-auto` | Tag applied to ingested notes |
| `OUTBOUND_TAG` | `send-to-supernote` | Tag that triggers export to Supernote |
| `OUTBOUND_SUBFOLDER` | `Paperless` | Subfolder inside `Document/` for exports |
| `POLL_INTERVAL` | `60` | Seconds between outbound sync polls |
| `STATE_DB` | `/state/bridge.db` | SQLite state file path |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`) |

## Docker Compose example

```yaml
supernote-bridge:
  build:
    context: ./supernote-paperless-sync
  container_name: supernote-bridge
  depends_on:
    - supernote-service
  networks:
    - net
    - documents_net
  volumes:
    - /path/to/supernote/data:/supernote/data
    - /path/to/supernote/convert:/supernote/convert:ro
    - /path/to/supernote-bridge:/state
  environment:
    PAPERLESS_URL: http://paperless-ngx:8000
    PAPERLESS_TOKEN: your_token_here
    SUPERNOTE_NOTE_DIR: /supernote/data/user@example.com/Supernote/Note
    SUPERNOTE_DOC_DIR: /supernote/data/user@example.com/Supernote/Document
    NOTELIB_CONVERT_DIR: /supernote/convert
  restart: unless-stopped
```

## Prerequisites

1. Both `paperless-gpt-ocr-auto` and `send-to-supernote` tags must exist in Paperless-ngx
2. The `documents_net` Docker network must be accessible from the Supernote stack
3. Create the state volume directory: `mkdir -p /path/to/supernote-bridge`

## License

Apache-2.0
