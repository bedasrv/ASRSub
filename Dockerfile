FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOME=/home/user DEBIAN_FRONTEND=noninteractive LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd -g 1000 asrsub && useradd -m -u 1000 -g 1000 -d /home/user asrsub

COPY --chown=asrsub:asrsub . /app
USER asrsub
CMD [python, orchestrator.py]
