FROM python:3.11-slim

WORKDIR /app

# Ensure logs stream immediately and no .pyc files are written
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

RUN chmod +x start.sh

EXPOSE 8000

# Run both agent worker + API in one container
CMD ["/bin/bash", "start.sh"]
