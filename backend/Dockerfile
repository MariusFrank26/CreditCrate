version: "3.8"

services:
  backend:
    build: ./backend
    container_name: creditcrate-backend
    restart: unless-stopped
    environment:
      - GENIUS_ACCESS_TOKEN=${GENIUS_ACCESS_TOKEN}
    ports:
      - "5001:5000" # Exponiert das Backend lokal auf Port 5001

  frontend:
    build: ./frontend
    container_name: creditcrate-frontend
    restart: unless-stopped
    depends_on:
      - backend
    ports:
      - "8085:80" # Exponiert das Frontend lokal auf Port 8085
