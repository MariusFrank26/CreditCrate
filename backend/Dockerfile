# 1. Schlankes Python-Basis-Image
FROM python:3.11-slim

# 2. Arbeitsverzeichnis im Container festlegen
WORKDIR /app

# 3. Pip aktualisieren
RUN pip install --no-cache-dir --upgrade pip

# 4. requirements.txt kopieren und Pakete installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Den restlichen Backend-Code kopieren
COPY . .

# 6. Flask-Port dokumentieren
EXPOSE 5000

# 7. App starten
# gunicorn: one worker, so the campaign sender runs in exactly one process. Threads serve requests.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:5000", "--timeout", "120", "--graceful-timeout", "25", "app:app"]
