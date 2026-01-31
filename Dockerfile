FROM python:3.9-slim

# Install system dependencies (FFmpeg, Aria2, 7zip)
RUN apt-get update && \
    apt-get install -y ffmpeg aria2 p7zip-full && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Start Command
CMD ["python", "main.py"]
