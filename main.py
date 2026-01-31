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
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
DUMP_ID = int(os.environ.get("DUMP_ID", 0)) # ID must start with -100
PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Initialize Aria2 ---
subprocess.Popen(['aria2c', '--enable-rpc', '--rpc-listen-port=6800', '--daemon'])
time.sleep(1)
aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))

# --- Globals ---
abort_dict = {}

# --- Limits ---
FREE_LIMIT = 300 * 1024 * 1024
PREM_LIMIT = 1500 * 1024 * 1024
YTDLP_LIMIT = 900 * 1024 * 1024

# --- Web Server ---
from aiohttp import web
async def web_server():
    async def handle(request): return web.Response(text="Bot Fixed & Running!")
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

async def update_progress_ui(current, total, message, start_time, action):
    now = time.time()
    diff = now - start_time
    if round(diff % 5.00) == 0 or current == total:
        percentage = current * 100 / total
        filled = int(percentage // 10)
        bar = '●' * filled + '○' * (10 - filled)
        speed = current / diff if diff > 0 else 0
        
        text = f"**{action}**\n"
        text += f"[{bar}] `{round(percentage, 2)}%`\n"
        text += f"💾 `{humanbytes(current)}` / `{humanbytes(total)}`\n"
        text += f"⚡ `{humanbytes(speed)}/s`"
        
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="cancel_task")]])
        try: await message.edit_text(text, reply_markup=buttons)
        except: pass

# --- Smart File Finder (For Torrents) ---
def find_largest_file(path):
    if os.path.isfile(path):
        return path
    
    # Agar folder hai to sabse badi file dhundo
    largest_file = None
    max_size = 0
    for root, dirs, files in os.walk(path):
        for file in files:
            file_path = os.path.join(root, file)
            size = os.path.getsize(file_path)
            if size > max_size:
                max_size = size
                largest_file = file_path
    return largest_file

# --- 1. Aria2 Downloader (Magnet + Torrent Files) ---
async def download_torrent(link, message, user_id):
    try:
        # Check if it's a URL or Magnet
        if link.startswith("http"):
            download = aria2.add_uris([link])
        else:
            download = aria2.add_magnet(link)
        
        start_time = time.time()
        
        while True:
            if user_id in abort_dict:
                aria2.remove([download])
                return "CANCELLED"

            download.update()
            
            # Error Check
            if download.status == "error":
                return None
            
            # Complete Check
            if download.status == "complete":
                # Torrent aksar folder banata hai, usme se main file nikaalo
                final_path = find_largest_file(download.files[0].path)
                return final_path
            
            # Progress
            if download.total_length > 0:
                await update_progress_ui(download.completed_length, download.total_length, message, start_time, "🧲 Leeching Torrent...")
            
            await asyncio.sleep(4)
    except Exception as e:
        print(f"Torrent Error: {e}")
        return None

# --- 2. yt-dlp Downloader ---
async def download_ytdlp(url, message, user_id):
    loop = asyncio.get_event_loop()
    start_time = time.time()
    
    def progress_hook(d):
        if user_id in abort_dict: raise Exception("Cancelled")
        if d['status'] == 'downloading':
            try:
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes', 0)
                if total > 0:
                    asyncio.run_coroutine_threadsafe(
                        update_progress_ui(downloaded, total, message, start_time, "📥 Downloading (yt-dlp)..."),
                        loop
                    )
            except: pass

    def run():
        opts = {
            'format': 'best',
            'outtmpl': '%(title)s.%(ext)s',
            'max_filesize': YTDLP_LIMIT,
            'quiet': True,
            'progress_hooks': [progress_hook]
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)

    try: return await loop.run_in_executor(None, run)
    except: return "CANCELLED"

# --- 3. Direct Downloader ---
async def download_direct(url, message, user_id):
    start_time = time.time()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200: return None
                total = int(resp.headers.get("content-length", 0))
                name = url.split("/")[-1].split("?")[0]
                if "." not in name: name += ".mp4"
                
                f = await aiofiles.open(name, mode='wb')
                downloaded = 0
                async for chunk in resp.content.iter_chunked(1024*1024):
                    if user_id in abort_dict:
                        await f.close()
                        os.remove(name)
                        return "CANCELLED"
                    await f.write(chunk)
                    downloaded += len(chunk)
                    await update_progress_ui(downloaded, total, message, start_time, "📥 Downloading...")
                await f.close()
                return name
    except: return None

# --- Cancel Callback ---
@app.on_callback_query(filters.regex("cancel_task"))
async def cancel_cb(client, cb):
    abort_dict[cb.from_user.id] = True
    await cb.message.edit_text("❌ Task Cancelled.")
    await cb.answer("Cancelled")

# --- Main Logic ---
@app.on_message(filters.text & filters.private)
async def main_handler(client, message):
    url = message.text
    user_id = message.from_user.id
    if user_id in abort_dict: del abort_dict[user_id]
    
    msg = await message.reply_text("🔄 **Processing...**")
    file_path = None

    try:
        # A. Torrent / Magnet Detection
        # Agar link .torrent par khatam ho ya magnet ho, to Torrent Downloader use karo
        if url.startswith("magnet:") or url.lower().endswith(".torrent"):
            file_path = await download_torrent(url, msg, user_id)
        
        # B. YouTube / Social Media
        elif any(x in url for x in ["youtube.com", "youtu.be", "hanime", "instagram"]):
            file_path = await download_ytdlp(url, msg, user_id)
        
        # C. Direct Link (baaki sab)
        elif url.startswith("http"):
            file_path = await download_direct(url, msg, user_id)

        if file_path == "CANCELLED": return

        if file_path and os.path.exists(file_path):
            await msg.edit_text("📤 **Uploading...**")
            sent_msg = await message.reply_document(
                document=file_path,
                caption=f"📂 `{os.path.basename(file_path)}`",
                progress=update_progress_ui,
                progress_args=(msg, time.time(), "📤 Uploading...")
            )
            
            # --- Dump Logic Fixed ---
            if DUMP_ID != 0:
                try:
                    await sent_msg.copy(DUMP_ID)
                except Exception as e:
                    # Agar dump fail ho, to user ko bata do (Debug ke liye)
                    await message.reply_text(f"⚠️ **Dump Error:** {e}\nCheck DUMP_ID & Admin Rights.")

            await msg.delete()
            if os.path.exists(file_path): os.remove(file_path)
            # Aria2 cleanup
            aria2.purge()
        else:
            await msg.edit_text("❌ Download Failed!")

    except Exception as e:
        await msg.edit_text(f"⚠️ Critical Error: {str(e)}")

if __name__ == "__main__":
    app.start()
    app.loop.run_until_complete(web_server())
    app.loop.run_forever()
