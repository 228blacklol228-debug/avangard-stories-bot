FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && \
    groupadd -g 10001 bot && useradd -u 10001 -g bot -M bot && \
    mkdir -p /app/data && chown bot:bot /app/data
COPY --chown=bot:bot stories_bot ./stories_bot
COPY --chown=bot:bot assets ./assets
USER bot
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
 CMD python -c "import os,time,pathlib; p=pathlib.Path(os.environ['DATA_DIR'])/'heartbeat'; assert p.exists() and time.time()-p.stat().st_mtime<240"
CMD ["python", "-m", "stories_bot"]
