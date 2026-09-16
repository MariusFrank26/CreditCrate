# Schlankes Nginx-Webserver-Image
FROM nginx:alpine

# Kopiert Ihren Frontend-Code in das Nginx-Standardverzeichnis
COPY . /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
