FROM python:3.10-slim

WORKDIR /app

# 1. Install Dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    aria2 \
    p7zip-full \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Rclone (Updated Method)
RUN curl https://rclone.org/install.sh | bash

# 3. Python Reqs
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# 4. Copy Code
COPY . .

# 5. Permissions & Start Command (Is line ko dhyan se copy karna)
RUN chmod +x start.sh
CMD ["bash", "start.sh"]
