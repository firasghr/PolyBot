# ---- PolyBot Backend Dockerfile ----
FROM python:3.12-slim

LABEL maintainer="PolyBot"
LABEL description="Polymarket copy-trading backend API"

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/ ./src/
COPY backend/ ./backend/

# Non-root user for security
RUN addgroup --system polybot && adduser --system --ingroup polybot polybot
USER polybot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
