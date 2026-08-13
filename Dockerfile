# 1. Schlankes Python-Basis-Image wählen
FROM python:3.11-slim

# 2. Arbeitsverzeichnis im Container festlegen
WORKDIR /app

# 3. System-Abhängigkeiten/Pip aktualisieren
RUN pip install --no-cache-dir --upgrade pip

# 4. Zuerst nur requirements.txt kopieren (nutzt Docker-Cache optimal)
COPY requirements.txt .

# 5. Python-Pakete installieren
RUN pip install --no-cache-dir -r requirements.txt

# 6. Den restlichen Code (app.py etc.) in den Container kopieren
COPY . .

# 7. Port dokumentieren, den Flask nutzt
EXPOSE 5000

# 8. Befehl zum Starten der App
CMD ["python", "app.py"]
