FROM python:3.11-slim

RUN useradd -m -u 1000 appuser

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libxml2-dev libxslt1-dev libffi-dev curl git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# newspaper3k is optional (has BeautifulSoup fallback) — install best-effort
RUN pip install --no-cache-dir --prefer-binary newspaper3k || true

# tvdatafeed: install from the maintained rongardF fork (the original PyPI
# package was withdrawn). git is installed above so pip can clone. This is
# critical — without it, tvdata.py silently returns None and NSE indices
# fall through to yfinance's 15-min-delayed quotes.
RUN pip install --no-cache-dir 'tvdatafeed @ git+https://github.com/rongardF/tvdatafeed.git' || true

# Pre-download NLTK data at build time
RUN python -c "import nltk; nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True)" || true

COPY . .

RUN mkdir -p /app/db && chown -R appuser:appuser /app

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:${PORT:-8001}/health || exit 1

CMD ["python", "run.py"]
