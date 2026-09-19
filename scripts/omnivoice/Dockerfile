# OmniVoice TTS server for Jarvis (scripts/omnivoice/server.py). Built and run on ardi.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir omnivoice==0.2.1 soundfile "fastapi<1" "uvicorn[standard]<1"
WORKDIR /app
COPY server.py /app/server.py
EXPOSE 9120
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9120"]
