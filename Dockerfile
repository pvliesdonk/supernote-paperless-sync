FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libcairo2-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Copy everything needed to build the package
COPY pyproject.toml README.md ./
COPY src/ src/

# Install the package and all dependencies
RUN pip install --no-cache-dir .

CMD ["python", "-m", "supernote_paperless_sync"]
