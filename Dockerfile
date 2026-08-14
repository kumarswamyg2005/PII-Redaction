# Hugging Face Spaces (Docker SDK) and local runs use the same image.
FROM python:3.11-slim

# Tesseract with Hindi: the Aadhaar card prints every field twice, in
# Devanagari and English, and reading only English leaves half the card.
# libgl1/libglib are OpenCV's runtime dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-hin \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Spaces runs as uid 1000; the model cache must be writable by that user.
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface \
    PYTHONUNBUFFERED=1 \
    UPLOAD_DIR=/tmp/uploads \
    OUTPUT_DIR=/tmp/outputs

WORKDIR /app

COPY --chown=user requirements.txt .

# Install PyTorch from the CPU index *first*. GLiNER depends on torch, and the
# default wheel drags in the whole CUDA stack — nvidia-cublas alone is 542 MB —
# for a container that will only ever run CPU inference. Pinning the CPU build
# here means the later resolve finds torch already satisfied.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m spacy download en_core_web_sm

USER user

# Bake the zero-shot model into the image so a cold Space does not download
# ~500 MB on its first request.
RUN python -c "from gliner import GLiNER; GLiNER.from_pretrained('knowledgator/gliner-pii-base-v1.0')"

COPY --chown=user . .

EXPOSE 7860

# One worker, several threads. The worker count stays at one deliberately: each
# worker loads its own copy of the ~500 MB zero-shot model, so a second one
# doubles the memory for no gain on a laptop. But a single *sync* worker serves
# exactly one request at a time, and a 126-page document occupies it for well
# over a minute — during which the page itself, the health check and any second
# upload are all refused, which a tunnel reports to the browser as 503.
# Threads share the loaded model and keep the app answering while a long job
# runs. The 600s timeout is for the job, not the connection.
CMD ["gunicorn", "--bind", "0.0.0.0:7860", \
     "--worker-class", "gthread", "--workers", "1", "--threads", "8", \
     "--timeout", "600", "--graceful-timeout", "30", \
     "--access-logfile", "-", "app:app"]
