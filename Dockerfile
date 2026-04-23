FROM python:3.11-slim

WORKDIR /app

# System deps for lxml, BeautifulSoup
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libxml2-dev libxslt1-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

CMD uvicorn dashboard_api:app --host 0.0.0.0 --port ${PORT:-8001}
