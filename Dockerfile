FROM python:3.11-slim

# Security: non-root user — app cannot write outside /app
RUN useradd -m -u 1000 appuser

WORKDIR /app

# System deps for lxml, newspaper3k, yfinance
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libxml2-dev libxslt1-dev libffi-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages (cached layer — only rebuilds if requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download NLTK data so container starts without hitting network
RUN python -c "import nltk; nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True)" || true

# Copy app source
COPY . .

# DB directory owned by appuser
RUN mkdir -p /app/db && chown -R appuser:appuser /app

# Drop to non-root
USER appuser

EXPOSE 8001

# Health check — Railway and local docker both use this
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

CMD ["python", "run.py"]
