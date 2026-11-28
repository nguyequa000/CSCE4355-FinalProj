# docker/app/Dockerfile
#
# Shared runtime for the Streamlit dashboard, the console notifier and the
# optional lock-contention demo. One image, three commands (see compose).
#
# Build context is the repository root because the dashboard imports
# scripts/dbconf.py and reads dashboard/app.py.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY docker/app/requirements.txt ./docker/app/requirements.txt
RUN pip install --no-cache-dir -r docker/app/requirements.txt

COPY . .

EXPOSE 8501
CMD ["streamlit", "run", "dashboard/app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", \
     "--server.headless=true", "--browser.gatherUsageStats=false"]
