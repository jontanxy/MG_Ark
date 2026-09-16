FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mg_archive_bot ./mg_archive_bot

VOLUME ["/app/data", "/app/secrets"]
ENV DATABASE_URL=sqlite:////app/data/mg_archive.sqlite3 \
    WORK_DIR=/app/data/work

CMD ["python", "-m", "mg_archive_bot"]
