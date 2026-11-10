# Image multi-architecture : fonctionne telle quelle sur Raspberry Pi OS 64 bits (arm64) et sur PC (amd64).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Europe/Paris
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --create-home --uid 1000 sharedfleet && mkdir /data && chown sharedfleet /data
USER sharedfleet
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/sante', timeout=3)"

# Un seul processus : il porte aussi les tâches de fond (SMS, synchronisation Getaround, relève des contraventions).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
