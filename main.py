import os
import time
import asyncio
import aiohttp
import aiofiles
import yt_dlp
import aria2p
import subprocess
import shutil
from urllib.parse import unquote
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
DUMP_ID = int(os.environ.get("DUMP_ID", 0))
PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Initialize Aria2 ---
subprocess.Popen(['aria2c', '--enable-rpc', '--rpc-listen-port=6800', '--daemon'])
time.sleep(1)
aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))

# --- Globals ---
# Abort Flag Dictionary: {user_id: True}
abort_dict = {}

# --- Limits ---
FREE_LIMIT = 300 * 1024 * 1024
PREM_LIMIT = 1500 * 1024 * 1024
YTDLP_LIMIT = 900 * 1024 * 1024

# --- Web Server ---
async def web_server():
    async def handle(request): return web.Response(text="Bot Running with Cancel Feature!")
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# --- Helper: Visuals ---
def humanbytes(size):
    if not size: return ""
    power = 2**10
    n = 0
    dic = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power: size /= power; n += 1
    return str(round(size, 2)) + " " + dic[n] + 'B'

def get_progress_bar_string(current, total):
    percentage = current * 100 / total
    filled_blocks = int(percentage // 10)
    empty_blocks = 10 - filled_blocks
    return '●' * filled_blocks + '○' * empty_blocks, percentage

async def update_progress_ui(current, total, message, start_time, action):
    now = time.time()
    diff = now - start_time
    if round(diff % 5.00) == 0 or current == total:
        bar, percentage = get_progress_bar_string(current, total)
        speed = current / diff if diff > 0 else 0
        text = f"**{action}**\n"
        text += f"[{bar}] `{round(percentage, 2)}%`\n"
        text += f"💾 `{humanbytes(current)}` / `{humanbytes(total)}`\n"
        text += f"⚡ `{humanbytes(speed)}/s`"
        
        # Cancel Button
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="cancel_task")]])
        
        try: await message.edit_text(text, reply_markup=buttons)
        except: pass

# --- 1. yt-dlp with Progress & Cancel ---
async def download_ytdlp(url, message, user_id):
    loop = asyncio.get_event_loop()
    start_time = time.time()
    
    # Progress Hook for yt-dlp
    def ytdlp_progress_hook(d):
        if user_id in abort_dict:
            raise Exception("Cancelled by User")
            
        if d['status'] == 'downloading':
            try:
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes', 0)
                if total > 0:
                    # Thread-safe UI update
                    future = asyncio.run_coroutine_threadsafe(
                        update_progress_ui(downloaded, total, message, start_time, "📥 Downloading (yt-dlp)..."),
                        loop
                    )
            except Exception as e:
                pass

    def run_dl():
        ydl_opts = {
            'format': 'best',
            'outtmpl': '%(title)s.%(ext)s',
            'max_filesize': YTDLP_LIMIT,
            'quiet': True,
            'progress_hooks': [ytdlp_progress_hook], # Attach hook
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)
        except Exception as e:
            if "Cancelled by User" in str(e): return "CANCELLED"
            return None

    return await loop.run_in_executor(None, run_dl)

# --- 2. Aria2 with Cancel ---
async def download_torrent(link, message, user_id):
    try:
        download = aria2.add_magnet(link)
        start_time = time.time()
        
        while True:
            # Check Cancel
            if user_id in abort_dict:
                aria2.remove([download])
                return "CANCELLED"

            download.update()
            if download.status == "complete":
                return download.files[0].path
            elif download.status == "error":
                return None
            
            if download.total_length > 0:
                await update_progress_ui(download.completed_length, download.total_length, message, start_time, "🧲 Leeching Torrent...")
            
            await asyncio.sleep(4)
    except: return None

# --- 3. Direct Link with Cancel ---
async def download_direct(url, message, user_id):
    start_time = time.time()
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                file_name = url.split("/")[-1].split("?")[0]
                if not "." in file_name: file_name += ".mp4"
                
                total_size = int(response.headers.get("content-length", 0))
                downloaded = 0
                
                f = await aiofiles.open(file_name, mode='wb')
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    # Check Cancel
                    if user_id in abort_dict:
                        await f.close()
                        os.remove(file_name)
                        return "CANCELLED"
                        
                    await f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        await update_progress_ui(downloaded, total_size, message, start_time, "📥 Downloading...")
                await f.close()
                return file_name
    return None

# --- Callback Handler (Cancel Button) ---
@app.on_callback_query(filters.regex("cancel_task"))
async def cancel_callback(client, callback_query):
    user_id = callback_query.from_user.id
    abort_dict[user_id] = True # Set Flag
    await callback_query.message.edit_text("❌ **Task Cancelled by User.**")
    await callback_query.answer("Cancelled!")

# --- Main Handler ---
@app.on_message(filters.text & filters.private)
async def main_handler(client, message):
    url = message.text
    user_id = message.from_user.id
    
    # Reset Abort Flag
    if user_id in abort_dict: del abort_dict[user_id]

    msg = await message.reply_text("🔄 **Initializing...**")
    file_path = None

    try:
        # Determine Downloader
        if "magnet:" in url:
            file_path = await download_torrent(url, msg, user_id)
        elif any(x in url for x in ["youtube.com", "youtu.be", "hanime.tv", "instagram.com"]):
            file_path = await download_ytdlp(url, msg, user_id)
        elif url.startswith("http"):
            file_path = await download_direct(url, msg, user_id)

        # Check Result
        if file_path == "CANCELLED":
            return # Message already edited in callback

        if file_path and os.path.exists(file_path):
            # Check Cancel again before Upload
            if user_id in abort_dict:
                await msg.edit_text("❌ **Cancelled before Upload.**")
                os.remove(file_path)
                return

            await msg.edit_text("📤 **Uploading...**")
            sent_msg = await message.reply_document(
                document=file_path,
                caption=f"🎥 `{os.path.basename(file_path)}`",
                progress=update_progress_ui,
                progress_args=(msg, time.time(), "📤 Uploading...")
            )
            
            # Dump Logic
            if DUMP_ID != 0:
                try: await sent_msg.copy(DUMP_ID)
                except: pass

            await msg.delete()
            os.remove(file_path)
        else:
            await msg.edit_text("❌ Download Failed! (Link Error or File too big)")

    except Exception as e:
        await msg.edit_text(f"⚠️ Error: {str(e)}")

if __name__ == "__main__":
    app.start()
    app.loop.run_until_complete(web_server())
    app.loop.run_forever()
    
            
            
