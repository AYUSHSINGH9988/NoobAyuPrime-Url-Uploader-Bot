FROM python:3.10-slim

WORKDIR /app

# 1. Install System Tools & Dependencies
# Added 'ca-certificates' to fix SSL errors
RUN apt-get update && apt-get install -y \
    ffmpeg \
    aria2 \
    p7zip-full \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Rclone (Manual Method - Error Free)
RUN curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip && \
    unzip rclone-current-linux-amd64.zip && \
    cd rclone-*-linux-amd64 && \
    cp rclone /usr/bin/ && \
    chown root:root /usr/bin/rclone && \
    chmod 755 /usr/bin/rclone && \
    cd .. && \
    rm -rf rclone-*-linux-amd64*

# 3. Install Python Requirements
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# 4. Copy Code & Start
COPY . .
CMD ["python3", "main.py"]
