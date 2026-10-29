# Pinned by digest so a rebuild cannot silently pull a different base image.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Fully pinned, hash-verified dependency set (regenerate with: pip-compile --generate-hashes requirements.txt -o requirements.lock)
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY mg_archive_bot ./mg_archive_bot

# Run as an unprivileged user. Only data/ and secrets/ belong to it; the code stays root-owned (read-only for the bot).
# The mounted data/ must be writable and secrets/ readable by uid 10001 — e.g. `chown -R 10001 data secrets` on the host.
RUN useradd --system --uid 10001 --create-home mgbot && mkdir -p /app/data /app/secrets && chown -R mgbot /app/data /app/secrets
USER mgbot

VOLUME ["/app/data", "/app/secrets"]
ENV DATABASE_URL=sqlite:////app/data/mg_archive.sqlite3 \
    WORK_DIR=/app/data/work

CMD ["python", "-m", "mg_archive_bot"]
