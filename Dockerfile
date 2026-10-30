# Standard library only: nothing to install, nothing to build.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY index.html admin.html roadmap.html server.py report.py mailer.py todoist.py ./
COPY data/ ./data/
RUN useradd --create-home --uid 10001 app && chown -R app /app
# Starts as root on purpose: Railway mounts volumes root-owned, so server.py
# takes the volume and drops to the app user (uid 10001) before serving.
EXPOSE 8080
CMD ["python", "server.py"]
