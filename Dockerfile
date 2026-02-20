FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

COPY src/ src/

CMD ["python", "-m", "supernote_paperless_sync"]
