import os
import time
import json
import asyncio
import aiohttp
import aiofiles
from urllib.parse import unquote
from pyrogram import Client, filters
from pyrogram.types import Message
from aiohttp import web

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0)) # Apna Telegram ID daalna zaroori hai
PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Memory Database (Temporary) ---
# Note: Render Free Tier restart hone par ye data reset ho jayega.
# Permanent ke liye MongoDB lagana padega.
users_db = {} 
active_tasks = {} # Format: {user_id: count}
PRICE_MESSAGE = "Contact Owner for Premium Prices."

# --- Constants ---
FREE_LIMIT_SIZE = 300 * 1024 * 1024  # 300 MB
PREM_LIMIT_SIZE = 650 * 1024 * 1024  # 650 MB
FREE_TASK_LIMIT = 1
PREM_TASK_LIMIT = 2

# --- Web Server ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is Running with Admin Panel!")
    server = web.Application()
    server.router.add_get("/", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server started on port {PORT}")

# --- Helper: User Management ---
def get_user(user_id):
    if user_id not in users_db:
        users_db[user_id] = {"premium": False, "banned": False}
    return users_db[user_id]

def update_user(user_id, key, value):
    if user_id not in users_db:
        users_db[user_id] = {"premium": False, "banned": False}
    users_db[user_id][key] = value

# --- Helper: Progress Bar ---
def humanbytes(size):
    if not size: return ""
    power = 2**10
    n = 0
    Dic_powerN = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + Dic_powerN[n] + 'B'

def time_formatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
        ((str(hours) + "h, ") if hours else "") + \
        ((str(minutes) + "m, ") if minutes else "") + \
        ((str(seconds) + "s") if seconds else "")
    return tmp[:-2] if tmp.endswith(", ") else tmp

async def progress(current, total, message, start_time, action_type):
    now = time.time()
    diff = now - start_time
    if round(diff % 8.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000
        
        filled_blocks = int(percentage // 10) 
        empty_blocks = 10 - filled_blocks
        bar = '●' * filled_blocks + '○' * empty_blocks
        
        text = f"**{action_type}**\n" 
        text += f"[{bar}]  `{round(percentage, 2)}%`\n\n"
        text += f"⚡ **Speed:** `{humanbytes(speed)}/s`\n"
        text += f"💾 **Done:** `{humanbytes(current)}` / `{humanbytes(total)}`\n"
        text += f"⏳ **ETA:** `{time_formatter(time_to_completion)}`"
        try:
            await message.edit_text(text)
        except:
            pass

# --- Downloader ---
async def download_file(url, message, start_time):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                file_name = None
                content_disposition = response.headers.get("Content-Disposition")
                if content_disposition:
                    try:
                        file_name = content_disposition.split('filename=')[1].strip('"')
                    except:
                        pass
                if not file_name:
                    file_name = url.split("/")[-1].split("?")[0]
                
                file_name = unquote(file_name)
                if not "." in file_name: file_name += ".mp4"

                total_size = int(response.headers.get("content-length", 0))
                downloaded_size = 0
                
                f = await aiofiles.open(file_name, mode='wb')
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    await f.write(chunk)
                    downloaded_size += len(chunk)
                    if total_size > 0:
                        await progress(downloaded_size, total_size, message, start_time, "📥 Downloading...")
                await f.close()
                return file_name, total_size
    return None, 0

# --- Admin/Owner Commands ---
@app.on_message(filters.command("addprem") & filters.user(OWNER_ID))
async def add_premium(client, message):
    try:
        user_id = int(message.command[1])
        update_user(user_id, "premium", True)
        await message.reply_text(f"✅ User `{user_id}` is now **Premium**!")
    except:
        await message.reply_text("Usage: `/addprem user_id`")

@app.on_message(filters.command("remprem") & filters.user(OWNER_ID))
async def remove_premium(client, message):
    try:
        user_id = int(message.command[1])
        update_user(user_id, "premium", False)
        await message.reply_text(f"❌ User `{user_id}` removed from Premium.")
    except:
        await message.reply_text("Usage: `/remprem user_id`")

@app.on_message(filters.command("ban") & filters.user(OWNER_ID))
async def ban_user(client, message):
    try:
        user_id = int(message.command[1])
        update_user(user_id, "banned", True)
        await message.reply_text(f"🚫 User `{user_id}` has been **BANNED**.")
    except:
        await message.reply_text("Usage: `/ban user_id`")

@app.on_message(filters.command("unban") & filters.user(OWNER_ID))
async def unban_user(client, message):
    try:
        user_id = int(message.command[1])
        update_user(user_id, "banned", False)
        await message.reply_text(f"✅ User `{user_id}` has been **UNBANNED**.")
    except:
        await message.reply_text("Usage: `/unban user_id`")

@app.on_message(filters.command("setprice") & filters.user(OWNER_ID))
async def set_price(client, message):
    global PRICE_MESSAGE
    if len(message.command) > 1:
        PRICE_MESSAGE = message.text.split(None, 1)[1]
        await message.reply_text(f"✅ Price message updated:\n\n{PRICE_MESSAGE}")
    else:
        await message.reply_text("Usage: `/setprice Your Message Here`")

@app.on_message(filters.command("users") & filters.user(OWNER_ID))
async def stats(client, message):
    await message.reply_text(f"📊 Total Users in Memory: {len(users_db)}")

# --- User Commands ---
@app.on_message(filters.command("start"))
async def start(client, message):
    user = get_user(message.from_user.id)
    status = "🌟 PREMIUM" if user['premium'] else "👤 FREE"
    text = f"👋 **Hello {message.from_user.first_name}!**\n\n" \
           f"Your Status: **{status}**\n\n" \
           f"**📝 Limits:**\n" \
           f"👤 Free: Max 300MB, 1 Task\n" \
           f"🌟 Premium: Max 650MB, 2 Tasks\n\n" \
           f"Send a link to start!"
    await message.reply_text(text)

@app.on_message(filters.command("plan") | filters.command("upgrade"))
async def plan_info(client, message):
    await message.reply_text(f"💎 **Premium Plan Info**\n\n{PRICE_MESSAGE}")

# --- Main Logic ---
@app.on_message(filters.text & filters.private)
async def upload_handler(client, message):
    user_id = message.from_user.id
    user_data = get_user(user_id)
    url = message.text

    # 1. Check Ban
    if user_data['banned']:
        await message.reply_text("🚫 You are BANNED from using this bot.")
        return

    if not url.startswith("http"):
        return

    # 2. Check Active Tasks
    current_tasks = active_tasks.get(user_id, 0)
    task_limit = PREM_TASK_LIMIT if user_data['premium'] else FREE_TASK_LIMIT
    
    if current_tasks >= task_limit:
        await message.reply_text(f"⚠️ **Busy!** You have reached your task limit ({task_limit}). Wait for completion.")
        return

    # 3. Size Check (HEAD Request)
    msg = await message.reply_text("🔄 **Checking Link...**")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url) as resp:
                size = int(resp.headers.get('content-length', 0))
    except:
        size = 0 # Cannot detect size
    
    size_limit = PREM_LIMIT_SIZE if user_data['premium'] else FREE_LIMIT_SIZE
    
    if size > size_limit:
        limit_mb = size_limit / (1024*1024)
        await msg.edit_text(f"❌ **File too Big!**\nYour Limit: {limit_mb} MB\nFile Size: {humanbytes(size)}\n\nType /plan to upgrade.")
        return

    # --- Start Task ---
    active_tasks[user_id] = current_tasks + 1
    start_time = time.time()
    file_path = None
    
    try:
        # Download
        file_path, actual_size = await download_file(url, msg, start_time)
        
        # Check size again after download (if HEAD failed)
        if actual_size > size_limit:
            await msg.edit_text(f"❌ **File too Big!** Detected after download.\nYour Limit: {limit_mb} MB")
            os.remove(file_path)
            return

        if file_path:
            await msg.edit_text(f"✅ Downloaded. **Uploading...**")
            up_start_time = time.time()
            
            await message.reply_document(
                document=file_path,
                caption=f"📂 `{file_path}`\n👤 Uploaded by: {message.from_user.mention}",
                progress=progress,
                progress_args=(msg, up_start_time, "📤 Uploading...")
            )
            await msg.delete()
            os.remove(file_path)
        else:
            await msg.edit_text("❌ Failed to Download.")

    except Exception as e:
        await msg.edit_text(f"⚠️ Error: {str(e)}")
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    finally:
        # Task complete - free up slot
        if user_id in active_tasks:
            active_tasks[user_id] -= 1

if __name__ == "__main__":
    print("Bot Started...")
    app.start()
    app.loop.run_until_complete(web_server())
    app.loop.run_forever()
    
