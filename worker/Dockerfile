FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libglib2.0-0 \
    libgl1 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps
COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code + config + assets
COPY worker/worker.py /app/worker.py
COPY config.json /app/config.json
COPY .env /app/.env
COPY assets /app/assets

EXPOSE 8000

CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "8000"]
