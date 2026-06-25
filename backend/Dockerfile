FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for spatial libraries (GDAL, GEOS, PROJ)
RUN apt-get update && apt-get install -y \
    build-essential \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the server script
COPY server.py .

# Create the data directory (where server.py will download files from R2)
RUN mkdir -p /app/datasets/cleaned_features

# Expose the FastAPI port
EXPOSE 7860

# Run the FastAPI server on port 7860 (Hugging Face default)
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
