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
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "1", "--timeout", "600", "app:app"]
