FROM searxng/searxng:latest

COPY settings.yml /usr/local/searxng/app-settings.yml
COPY configure.py /usr/local/searxng/configure.py
ENTRYPOINT ["python", "/usr/local/searxng/configure.py"]

EXPOSE 8080

ENV SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml
