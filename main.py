import os
import time
import asyncio
import aiohttp
import aiofiles
import yt_dlp
import aria2p
import subprocess
import shutil
import traceback
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
MONGO_URL = os.environ.get("MONGO_URL")
PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- MongoDB Setup ---
if not MONGO_URL:
    print("❌ MONGO_URL Missing!")
    mongo_db = None
else:
    mongo_client = AsyncIOMotorClient(MONGO_URL)
    mongo_db = mongo_client["URL_Uploader_Bot"]
    config_col = mongo_db["config"]
    users_col = mongo_db["users"]

# --- Initialize Aria2 ---
try:
    subprocess.Popen(['aria2c', '--enable-rpc', '--rpc-listen-port=6800', '--daemon'])
    time.sleep(1)
    aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))
except Exception as e:
    print(f"Aria2 Error: {e}")

# --- Globals ---
abort_dict = {}
processing_ids = []
YTDLP_LIMIT = 2000 * 1024 * 1024 # 2GB Limit

# --- Helper Functions ---
def humanbytes(size):
    if not size: return "0B"
    power = 2**10
    n = 0
    dic = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power: size /= power; n += 1
    return str(round(size, 2)) + " " + dic[n] + 'B'

def time_formatter(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return "{:02d}:{:02d}:{:02d}".format(int(hours), int(minutes), int(seconds))

async def take_screenshot(video_path):
    try:
        thumb_path = f"{video_path}.jpg"
        cmd = ["ffmpeg", "-ss", "00:00:01", "-i", video_path, "-vframes", "1", "-q:v", "2", thumb_path, "-y"]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        if os.path.exists(thumb_path): return thumb_path
    except: pass
    return None

# --- UI Progress ---
async def update_progress_ui(current, total, message, start_time, action, download_obj=None):
    now = time.time()
    diff = now - start_time
    if round(diff % 5.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        elapsed = round(diff)
        eta = round((total - current) / speed) if speed > 0 else 0
        
        filled = int(percentage // 10)
        bar = '☁️' * filled + '◌' * (10 - filled)
        
        text = f"⚡ [Powered by Ayuprime](tg://user?id=8428298917)\n\n"
        text += f"**{action}**\n\n"
        text += f"{bar}  `{round(percentage, 1)}%`\n\n"
        text += f"💾 **Size:** `{humanbytes(current)}` / `{humanbytes(total)}`\n"
        text += f"🚀 **Speed:** `{humanbytes(speed)}/s`\n"
        text += f"⏳ **ETA:** `{time_formatter(eta)}`\n"
        
        if download_obj:
            try: text += f"🌱 **Seeds:** `{download_obj.num_seeders}`"
            except: pass

        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="cancel_task")]])
        try: await message.edit_text(text, reply_markup=buttons)
        except: pass

# --- Extraction Logic ---
def extract_archive(file_path):
    output_dir = f"extracted_{int(time.time())}"
    if not os.path.exists(output_dir): os.makedirs(output_dir)
    
    if not shutil.which("7z"): return [], None, "7z not installed!"

    cmd = ["7z", "x", file_path, f"-o{output_dir}", "-y"]
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    if process.returncode != 0: return [], output_dir, f"Error: {process.stderr.decode()}"

    files_list = []
    for root, dirs, files in os.walk(output_dir):
        for file in files: files_list.append(os.path.join(root, file))
    
    return files_list, output_dir, None

def get_files_from_folder(folder_path):
    files_list = []
    for root, dirs, files in os.walk(folder_path):
        for file in files: files_list.append(os.path.join(root, file))
    return files_list

# --- Upload Helper ---
async def upload_file(client, message, file_path, user_mention):
    try:
        file_name = os.path.basename(file_path)
        thumb_path = None
        is_video = file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv'))
        if is_video: thumb_path = await take_screenshot(file_path)
        
        caption = f"☁️ **File:** `{file_name}`\n📦 **Size:** `{humanbytes(os.path.getsize(file_path))}`\n👤 **User:** {user_mention}"
        
        await message.reply_document(
            document=file_path, caption=caption, thumb=thumb_path,
            force_document=False, progress=update_progress_ui,
            progress_args=(message, time.time(), "☁️ Uploading...", None)
        )
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
        return True
    except Exception as e:
        print(f"Upload Error: {e}")
        return False

# --- Download Logic ---
async def download_logic(url, message, user_id, mode):
    # Pixeldrain Fix
    if "pixeldrain.com/u/" in url:
        try: url = f"https://pixeldrain.com/api/file/{url.split('pixeldrain.com/u/')[1].split('/')[0]}"
        except: pass

    try:
        file_path = None
        
        # 1. Torrent / Magnet
        if mode == "leech" or url.startswith("magnet:") or url.lower().endswith(".torrent"):
            download = None
            if url.startswith("http"):
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(url) as res:
                        if res.status == 200:
                            data = await res.read()
                            meta = f"meta_{user_id}.torrent"
                            with open(meta, "wb") as f: f.write(data)
                            download = aria2.add_torrent(meta)
            else: download = aria2.add_magnet(url)
            
            if not download: return "ERROR: Failed to add torrent."
            
            start_time = time.time()
            while True:
                if user_id in abort_dict: aria2.remove([download]); return "CANCELLED"
                download.update()
                if download.status == "error": return "ERROR: Aria2 Error (Dead link/Full Storage)."
                if download.status == "complete": 
                    file_path = download.files[0].path; await asyncio.sleep(2); break
                if download.total_length > 0:
                     await update_progress_ui(download.completed_length, download.total_length, message, start_time, "☁️ Leeching...", download)
                await asyncio.sleep(4)

        # 2. yt-dlp (UPDATED FOR HANIME)
        elif mode == "ytdl" or any(x in url for x in ["youtube", "youtu.be", "hanime", "instagram"]):
             loop = asyncio.get_event_loop()
             def run():
                 # Adding Browser Headers to fix "Unsupported URL"
                 opts = {
                     'format': 'best',
                     'outtmpl': '%(title)s.%(ext)s',
                     'max_filesize': YTDLP_LIMIT,
                     'quiet': True,
                     'nocheckcertificate': True,
                     'http_headers': {
                         'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                         'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                         'Accept-Language': 'en-us,en;q=0.5'
                     }
                 }
                 with yt_dlp.YoutubeDL(opts) as ydl:
                     info = ydl.extract_info(url, download=True)
                     return ydl.prepare_filename(info)
             file_path = await loop.run_in_executor(None, run)

        # 3. Direct
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        total = int(resp.headers.get("content-length", 0))
                        name = url.split("/")[-1].split("?")[0]
                        if "pixeldrain" in url: name = "pixeldrain_file"
                        if "." not in name: name += ".mp4"
                        file_path = name
                        f = await aiofiles.open(file_path, mode='wb')
                        dl_size = 0; start_time = time.time()
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            if user_id in abort_dict: await f.close(); os.remove(file_path); return "CANCELLED"
                            await f.write(chunk)
                            dl_size += len(chunk)
                            await update_progress_ui(dl_size, total, message, start_time, "☁️ Downloading...", None)
                        await f.close()
        return file_path
    except Exception as e: return f"ERROR: {str(e)}"

# --- Main Processor ---
async def process_task(client, message, url, mode="auto"):
    user_id = message.from_user.id
    if message.id in processing_ids: return # Anti-Dup
    processing_ids.append(message.id)

    try:
        # Auto-Save User (MongoDB)
        if mongo_db is not None:
             await users_col.update_one({"_id": user_id}, {"$set": {"active": True}}, upsert=True)

        if user_id in abort_dict: del abort_dict[user_id]
        
        msg = await message.reply_text("☁️ **Connecting...**")
        file_path = await download_logic(url, msg, user_id, mode)
        
        if str(file_path).startswith("ERROR"):
            await msg.edit_text(f"❌ **Failed!**\nReason: `{file_path}`")
            return
        
        if file_path == "CANCELLED": await msg.edit_text("❌ Cancelled."); return
        if not file_path or not os.path.exists(file_path): await msg.edit_text("❌ Download Failed."); return
        
        final_files = []; temp_dir = None; is_extracted = False
        
        if os.path.isdir(file_path):
            await msg.edit_text("📂 **Processing Folder...**")
            final_files = get_files_from_folder(file_path)
        elif file_path.lower().endswith((".zip", ".rar", ".7z", ".tar")):
            await msg.edit_text("📦 **Extracting Archive...**")
            extracted_list, temp_dir, error_msg = extract_archive(file_path)
            if error_msg:
                await msg.edit_text(f"⚠️ Extract Failed: `{error_msg}`\nUploading zip...")
                final_files = [file_path]
            else:
                final_files = extracted_list; is_extracted = True; os.remove(file_path)
        else: final_files = [file_path]
        
        if not final_files: await msg.edit_text("❌ No files found."); return
        
        await msg.edit_text(f"☁️ **Uploading {len(final_files)} Files...**")
        
        for f in final_files:
            if os.path.getsize(f) < 1024*10: continue
            await upload_file(client, msg, f, message.from_user.mention)
            if is_extracted or os.path.isdir(file_path): 
                try: os.remove(f)
                except: pass
                
        await msg.delete()
        if temp_dir and os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        if os.path.isdir(file_path): shutil.rmtree(file_path)
        elif os.path.exists(file_path): os.remove(file_path)
        aria2.purge()
        
    except Exception as e:
        await message.reply_text(f"⚠️ Error: `{str(e)}`")
        traceback.print_exc()
    finally:
        if message.id in processing_ids: processing_ids.remove(message.id)

# --- Commands ---
@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    caption = "**👋 Bot Started!**\n⚡ [Powered by Ayuprime](tg://user?id=8428298917)\n\nSend any link to download."
    try: await m.reply_photo(photo="start_img.jpg", caption=caption)
    except: await m.reply_text(caption)

@app.on_message(filters.command("leech"))
async def leech_cmd(c, m): 
    link = m.text.split(None, 1)[1] if len(m.command)>1 else (m.reply_to_message.text if m.reply_to_message else "")
    if link: await process_task(c, m, link, "leech")

@app.on_message(filters.command("ytdl"))
async def ytdl_cmd(c, m):
    link = m.text.split(None, 1)[1] if len(m.command)>1 else (m.reply_to_message.text if m.reply_to_message else "")
    if link: await process_task(c, m, link, "ytdl")

@app.on_message(filters.text & filters.private)
async def auto_cmd(c, m):
    if not m.text.startswith("/") and (m.text.startswith("http") or m.text.startswith("magnet:")):
        await process_task(c, m, m.text, "auto")

@app.on_callback_query(filters.regex("cancel_task"))
async def cancel(c, cb): abort_dict[cb.from_user.id] = True; await cb.answer("Cancelling...")

# --- Web Server ---
from aiohttp import web
async def web_server():
    async def handle(request): return web.Response(text="Bot Running")
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

if __name__ == "__main__":
    app.start(); app.loop.run_until_complete(web_server()); app.loop.run_forever()
        
