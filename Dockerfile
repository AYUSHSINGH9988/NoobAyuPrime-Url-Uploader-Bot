FROM python:3.10-slim-buster

WORKDIR /app

# Install Dependencies + Rclone
RUN apt-get update && apt-get install -y \
    ffmpeg aria2 p7zip-full curl \
    && curl https://rclone.org/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "main.py"]
