FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY pyproject.toml .

# Stretch S2: the ctypes-bound min/max decimator. gcc is installed, used, and removed in
# one layer so it is not in the shipped image. If this step is ever dropped the app still
# runs -- decimation.py falls back to numpy when the .so is missing, by design.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && cc -O2 -shared -fPIC -o backend/native/libminmax.so backend/native/minmax.c \
 && apt-get purge -y gcc libc6-dev \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

EXPOSE 8000 8765

# -w 1 is load-bearing, not a default: acquisition state lives in this process's memory
# and a second worker would be a second, independent copy of it. See the README.
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "4", "-b", "0.0.0.0:8000", "backend.app:create_app()"]
