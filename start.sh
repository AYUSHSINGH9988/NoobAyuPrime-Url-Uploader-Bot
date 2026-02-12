#!/bin/bash

# 1. Start Aria2c Daemon in Background
echo "🚀 Starting Aria2c..."
aria2c --enable-rpc --rpc-listen-port=6800 --daemon --allow-overwrite=true --seed-time=0

# 2. Wait 3 seconds to ensure it's running
sleep 3

# 3. Start the Bot
echo "🤖 Starting Bot..."
python3 main.py
