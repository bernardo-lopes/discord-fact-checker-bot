FROM python:3.12-slim

# ffmpeg samples keyframes out of the videos attached to posts and strips their
# audio for transcription. tzdata is for the TZ setting and the Lisbon midnight
# status rotation.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/home/verdade/.cache/huggingface

RUN useradd --create-home --uid 1000 verdade
WORKDIR /app

# Dependencies first, so editing the bot's code doesn't reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Local speech-to-text. Adds a few hundred MB and a few minutes to the build,
# so it is opt-out: set WITH_WHISPER=false if you use the OpenAI backend or no audio.
ARG WITH_WHISPER=true
COPY requirements-local-whisper.txt .
RUN if [ "$WITH_WHISPER" = "true" ]; then \
        pip install --no-cache-dir -r requirements-local-whisper.txt; \
    fi

COPY bot.py invite.py test_fetch.py ./
COPY factchecker/ ./factchecker/

RUN mkdir -p /app/data /home/verdade/.cache \
 && chown -R verdade:verdade /app /home/verdade
USER verdade

VOLUME ["/app/data"]

# The bot touches this file every minute while its Discord connection is alive,
# so `docker ps` can tell "running" apart from "wedged".
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import os,sys,time; f='/tmp/heartbeat'; sys.exit(0 if os.path.exists(f) and time.time()-os.path.getmtime(f) < 300 else 1)"

CMD ["python", "-u", "bot.py"]
