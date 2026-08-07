FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create data directory
RUN mkdir -p /app/data

# Environment
ENV DB_PATH=/app/data/smc_bot.db
ENV PYTHONUNBUFFERED=1

# Run
CMD ["python", "main.py"]
