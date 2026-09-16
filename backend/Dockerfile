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
CMD ["python", "app.py"]
