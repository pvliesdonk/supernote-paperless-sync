FROM python:3.12-slim

WORKDIR /app

# pycairo (transitive dep of supernotelib) needs gcc + cairo headers to build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libcairo2-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

COPY src/ src/

CMD ["python", "-m", "supernote_paperless_sync"]
