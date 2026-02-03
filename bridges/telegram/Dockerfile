FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY bridges/telegram/pyproject.toml ./
RUN pip install --no-cache-dir aiohttp python-telegram-bot watchdog

# Copy bridge code
COPY bridges/telegram/ ./bridges/telegram/

ENV PYTHONPATH=/app/bridges/telegram/src
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "tether.bridges.telegram.main"]
