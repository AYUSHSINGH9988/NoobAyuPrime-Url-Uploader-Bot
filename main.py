import os
import time
import json
import asyncio
import aiohttp
import aiofiles
import yt_dlp
from urllib.parse import unquote
from pyrogram import Client, filters
from aiohttp import web

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Memory Database ---
users_db = {} 
active_tasks = {}
PRICE_MESSAGE = "Contact Owner for Premium Prices."

# --- Limits ---
FREE_LIMIT_SIZE = 300 * 1024 * 1024
PREM_LIMIT_SIZE = 650 * 1024 * 1024
YTDLP_LIMIT_SIZE = 500 * 1024 * 1024 # 500MB Limit for yt-dlp
FREE_TASK_LIMIT = 1
PREM_TASK_LIMIT = 2

# --- Web Server ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is Running with Pixeldrain & yt-dlp!")
    server = web.Application()
    server.router.add_get("/", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server started on port {PORT}")

# --- Helper Functions ---
def get_user(user_id):
    if user_id not in users_db:
        users_db[user_id] = {"premium": False, "banned": False}
    return users_db[user_id]

def update_user(user_id, key, value):
    if user_id not in users_db: users_db[user_id] = {"premium": False, "banned": False}
    users_db[user_id][key] = value

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
        speed = current / diff if diff > 0 else 0
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000 if speed > 0 else 0
        
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

# --- 1. Normal Downloader (Direct Links + Pixeldrain) ---
async def download_file(url, message, start_time):
    # Pixeldrain Auto-Convert Logic
    if "pixeldrain.com/u/" in url:
        file_id = url.split("pixeldrain.com/u/")[1].split("/")[0]
        url = f"https://pixeldrain.com/api/file/{file_id}"
    
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

# --- 2. yt-dlp Downloader (YouTube, Insta, etc) ---
async def download_ytdlp(url, message):
    # Running blocking code in a separate thread
    loop = asyncio.get_event_loop()
    
    def run_download():
        ydl_opts = {
            'format': 'best[ext=mp4]/best', # Best MP4 to avoid ffmpeg merging issues
            'outtmpl': '%(title)s.%(ext)s',
            'max_filesize': YTDLP_LIMIT_SIZE, # 500MB Limit
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename

    try:
        # Show static message because yt-dlp progress is hard to sync perfectly in thread
        await message.edit_text("📥 **Downloading via yt-dlp...**\n\n(Progress bar not available for yt-dlp links)\nPlease Wait...")
        file_path = await loop.run_in_executor(None, run_download)
        return file_path
    except Exception as e:
        print(f"Yt-dlp Error: {e}")
        return None

# --- Admin Commands ---
@app.on_message(filters.command("addprem") & filters.user(OWNER_ID))
async def add_premium(client, message):
    try:
        user_id = int(message.command[1])
        update_user(user_id, "premium", True)
        await message.reply_text(f"✅ User `{user_id}` is now **Premium**!")
    except: await message.reply_text("Usage: `/addprem user_id`")

@app.on_message(filters.command("remprem") & filters.user(OWNER_ID))
async def remove_premium(client, message):
    try:
        user_id = int(message.command[1])
        update_user(user_id, "premium", False)
        await message.reply_text(f"❌ User `{user_id}` removed from Premium.")
    except: await message.reply_text("Usage: `/remprem user_id`")

# --- Main Handler ---
@app.on_message(filters.text & filters.private)
async def upload_handler(client, message):
    user_id = message.from_user.id
    user_data = get_user(user_id)
    url = message.text

    if user_data['banned']:
        await message.reply_text("🚫 You are BANNED.")
        return
    if not url.startswith("http"):
        return

    # Task Check
    current_tasks = active_tasks.get(user_id, 0)
    task_limit = PREM_TASK_LIMIT if user_data['premium'] else FREE_TASK_LIMIT
    if current_tasks >= task_limit:
        await message.reply_text(f"⚠️ **Busy!** Task limit reached ({task_limit}).")
        return

    msg = await message.reply_text("🔄 **Processing...**")
    active_tasks[user_id] = current_tasks + 1
    
    file_path = None
    
    try:
        # 1. Check if URL is supported by yt-dlp (YouTube, Insta, FB, etc)
        # We assume common social sites are for yt-dlp, direct links for standard
        is_ytdlp = False
        ytdlp_domains = ["youtube.com", "youtu.be", "instagram.com", "facebook.com", "twitter.com", "x.com", "tiktok.com"]
        
        if any(domain in url for domain in ytdlp_domains):
            is_ytdlp = True
            file_path = await download_ytdlp(url, msg)
        else:
            # 2. Standard Download (includes Pixeldrain logic)
            start_time = time.time()
            file_path, size = await download_file(url, msg, start_time)
            
            # Size Check for direct links
            size_limit = PREM_LIMIT_SIZE if user_data['premium'] else FREE_LIMIT_SIZE
            if size > size_limit:
                await msg.edit_text(f"❌ **File too Big!** Limit: {size_limit/(1024*1024):.1f}MB")
                if file_path: os.remove(file_path)
                file_path = None

        # 3. Upload Logic
        if file_path and os.path.exists(file_path):
            # Check file size before upload (for yt-dlp files)
            file_size = os.path.getsize(file_path)
            limit_to_check = PREM_LIMIT_SIZE if user_data['premium'] else FREE_LIMIT_SIZE
            
            if file_size > limit_to_check:
                await msg.edit_text(f"❌ **File too Big!**\nDetected: {humanbytes(file_size)}\nLimit: {humanbytes(limit_to_check)}")
                os.remove(file_path)
            else:
                await msg.edit_text("✅ Downloaded. **Uploading...**")
                up_start_time = time.time()
                await message.reply_document(
                    document=file_path,
                    caption=f"📂 `{os.path.basename(file_path)}`\n👤 {message.from_user.first_name}",
                    progress=progress,
                    progress_args=(msg, up_start_time, "📤 Uploading...")
                )
                await msg.delete()
                os.remove(file_path)
        elif not is_ytdlp:
            # Error msg only if standard DL failed (yt-dlp handles its own errors)
            await msg.edit_text("❌ Download Failed!")

    except Exception as e:
        await msg.edit_text(f"⚠️ Error: {str(e)}")
        if file_path and os.path.exists(file_path): os.remove(file_path)
    finally:
        if user_id in active_tasks: active_tasks[user_id] -= 1

if __name__ == "__main__":
    print("Bot Started...")
    app.start()
    app.loop.run_until_complete(web_server())
    app.loop.run_forever()
    
