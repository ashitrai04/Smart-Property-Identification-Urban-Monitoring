FROM python:3.11-slim

# OpenCV runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face runs Docker Spaces as uid 1000
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY requirements.txt .
# CPU-only torch keeps the image ~1 GB smaller than the CUDA build
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY --chown=user app.py final_best.pt ./

USER user
ENV YOLO_CONFIG_DIR=/tmp/Ultralytics \
    OMP_NUM_THREADS=2

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
