FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py app.py
COPY wsgi_entry.py wsgi_entry.py
COPY gunicorn_config.py gunicorn_config.py
COPY entrypoint.sh entrypoint.sh
COPY templates/ templates/
COPY static/ static/

# Create directories for runtime data (persisted via volumes)
RUN mkdir -p /app/data /app/documents && chmod 755 /app/data /app/documents

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
