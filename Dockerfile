FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.lock requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini .

ARG COMMIT_SHA=unknown
RUN python -c "import os; from pathlib import Path; from src.version import BuildInfo; Path('src/build_info.json').write_text(BuildInfo(commit_sha=os.environ['COMMIT_SHA']).model_dump_json())"

# Create non-root user
RUN adduser --disabled-password --gecos "" appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
