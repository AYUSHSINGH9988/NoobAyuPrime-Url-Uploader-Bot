import os
import time
import json
import asyncio
import aiohttp
import aiofiles
import yt_dlp
import aria2p
import subprocess
import shutil
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Initialize Aria2 ---
subprocess.Popen(['aria2c', '--enable-rpc', '--rpc-listen-port=6800', '--daemon'])
time.sleep(1)
aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))

# --- Globals & Storage ---
abort_dict = {}
YTDLP_LIMIT = 1500 * 1024 * 1024
CONFIG_FILE = "config.json"
config = {"auth_users": [], "dump_id": 0}

# --- Load/Save Config ---
def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
        except: pass

def save_config():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f)
    except: pass

load_config()

# --- Web Server ---
from aiohttp import web
async def web_server():
    async def handle(request): return web.Response(text="Bot is Running!")
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# --- Helper: Visuals ---
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

# --- Thumbnail & Duration ---
async def take_screenshot(video_path):
    try:
        thumb_path = f"{video_path}.jpg"
        cmd = ["ffmpeg", "-ss", "00:00:01", "-i", video_path, "-vframes", "1", "-q:v", "2", thumb_path, "-y"]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        if os.path.exists(thumb_path): return thumb_path
    except: pass
    return None

async def get_duration(video_path):
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        return int(float(stdout.decode().strip()))
    except: return 0

# --- ☁️ CLOUD UI PROGRESS (Updated with Credit) ☁️ ---
async def update_progress_ui(current, total, message, start_time, action):
    now = time.time()
    diff = now - start_time
    if round(diff % 5.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        elapsed = round(diff)
        eta = round((total - current) / speed) if speed > 0 else 0
        
        filled = int(percentage // 10)
        bar = '☁️' * filled + '◌' * (10 - filled)
        
        # Added Powered by link at the top
        text = f"⚡ [Powered by Ayuprime](tg://user?id=8428298917)\n\n"
        text += f"**{action}**\n\n"
        text += f"{bar}  `{round(percentage, 1)}%`\n\n"
        text += f"💾 **Size:** `{humanbytes(current)}` / `{humanbytes(total)}`\n"
        text += f"🚀 **Speed:** `{humanbytes(speed)}/s`\n"
        text += f"⏳ **ETA:** `{time_formatter(eta)}`"
        
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="cancel_task")]])
        try: await message.edit_text(text, reply_markup=buttons)
        except: pass

# --- Extraction & File Logic ---
def extract_archive(file_path):
    output_dir = f"extracted_{int(time.time())}"
    if not os.path.exists(output_dir): os.makedirs(output_dir)
    cmd = ["7z", "x", file_path, f"-o{output_dir}", "-y"]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    files_list = []
    for root, dirs, files in os.walk(output_dir):
        for file in files: files_list.append(os.path.join(root, file))
    return files_list, output_dir

def get_files_from_folder(folder_path):
    files_list = []
    for root, dirs, files in os.walk(folder_path):
        for file in files: files_list.append(os.path.join(root, file))
    return files_list

# --- Smart Upload Helper ---
async def upload_file(client, message, file_path, user_mention):
    try:
        file_name = os.path.basename(file_path)
        thumb_path = None
        is_video = file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv'))
        if is_video: thumb_path = await take_screenshot(file_path)
        
        caption = f"☁️ **File:** `{file_name}`\n📦 **Size:** `{humanbytes(os.path.getsize(file_path))}`\n👤 **User:** {user_mention}"
        
        sent_msg = await message.reply_document(
            document=file_path, caption=caption, thumb=thumb_path,
            force_document=False, progress=update_progress_ui,
            progress_args=(message, time.time(), "☁️ Uploading...")
        )
        
        if config["dump_id"] != 0:
            try: await sent_msg.copy(config["dump_id"])
            except Exception as e: await message.reply_text(f"⚠️ **Dump Failed:** {e}")
            
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
        return True
    except Exception as e:
        print(f"Upload Error: {e}")
        return False

# --- Download Logic ---
async def download_logic(url, message, user_id, mode):
    if "pixeldrain.com/u/" in url:
        try: url = f"https://pixeldrain.com/api/file/{url.split('pixeldrain.com/u/')[1].split('/')[0]}"
        except: pass
    try:
        file_path = None
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
            if not download: return None
            start_time = time.time()
            while True:
                if user_id in abort_dict: aria2.remove([download]); return "CANCELLED"
                download.update()
                if download.status == "error": return None
                if download.status == "complete": 
                    file_path = download.files[0].path; break
                if download.total_length > 0:
                     await update_progress_ui(download.completed_length, download.total_length, message, start_time, "☁️ Leeching...")
                await asyncio.sleep(4)
        elif mode == "ytdl" or any(x in url for x in ["youtube", "youtu.be", "hanime", "instagram"]):
             loop = asyncio.get_event_loop()
             def run():
                 opts = {'format': 'best', 'outtmpl': '%(title)s.%(ext)s', 'max_filesize': YTDLP_LIMIT, 'quiet': True}
                 with yt_dlp.YoutubeDL(opts) as ydl:
                     info = ydl.extract_info(url, download=True)
                     return ydl.prepare_filename(info)
             file_path = await loop.run_in_executor(None, run)
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        total = int(resp.headers.get("content-length", 0))
                        name = "downloaded_file"
                        if "pixeldrain" in url: name = "pixeldrain_file"
                        cd = resp.headers.get("Content-Disposition")
                        if cd:
                            try: name = cd.split('filename=')[1].strip('"')
                            except: pass
                        else: name = url.split("/")[-1].split("?")[0]
                        if "." not in name: name += ".mp4"
                        file_path = name
                        f = await aiofiles.open(file_path, mode='wb')
                        dl_size = 0; start_time = time.time()
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            if user_id in abort_dict: await f.close(); os.remove(file_path); return "CANCELLED"
                            await f.write(chunk)
                            dl_size += len(chunk)
                            await update_progress_ui(dl_size, total, message, start_time, "☁️ Downloading...")
                        await f.close()
        return file_path
    except Exception as e: print(e); return None

# --- Main Processor ---
async def process_task(client, message, url, mode="auto"):
    user_id = message.from_user.id
    if user_id != OWNER_ID and user_id not in config["auth_users"]:
        await message.reply_text("⛔ **Unauthorized!** Ask Owner.")
        return
    if user_id in abort_dict: del abort_dict[user_id]
    msg = await message.reply_text("☁️ **Connecting to Cloud...**")
    file_path = await download_logic(url, msg, user_id, mode)
    if file_path == "CANCELLED": await msg.edit_text("❌ Cancelled."); return
    if not file_path or not os.path.exists(file_path): await msg.edit_text("❌ Download Failed."); return
    final_files = []; temp_dir = None; is_extracted = False
    if os.path.isdir(file_path):
        await msg.edit_text("📂 **Processing Folder...**")
        final_files = get_files_from_folder(file_path)
    elif file_path.lower().endswith((".zip", ".rar", ".7z", ".tar")):
        await msg.edit_text("📦 **Extracting Archive...**")
        extracted_list, temp_dir = extract_archive(file_path)
        if extracted_list: final_files = extracted_list; is_extracted = True; os.remove(file_path)
        else: final_files = [file_path]
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

# --- Commands ---
# NEW START COMMAND WITH PHOTO
@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    # Make sure 'start_img.jpg' is uploaded to your GitHub repo root!
    photo_path = "start_img.jpg" 
    caption = """
**👋 Welcome to URL Uploader Bot!**
⚡ [Powered by Ayuprime](tg://user?id=8428298917)

**🌟 Features:**
☁️ **Cloud Uploader:** Direct links & PixelDrain.
⚡ **Torrent Leech:** Magnets & .torrent files supported.
📹 **Video Downloader:** YouTube, Instagram, Hanime, etc.
📦 **Auto-Extract:** Unzips ZIP/RAR/7Z archives automatically.
🖼️ **Smart Metadata:** Adds thumbnails & duration to videos.
🔒 **Private Mode:** Authorized users only.
📝 **Dump Channel:** Backs up files to channel (Owner only).

*Send any link to start!*
    """
    try:
        await m.reply_photo(photo=photo_path, caption=caption)
    except Exception as e:
        # Fallback if photo is not found in repo
        await m.reply_text(caption.replace("**👋 Welcome", "⚠️ (Photo missing in repo)\n\n**👋 Welcome"))

@app.on_message(filters.command("setchatid") & filters.user(OWNER_ID))
async def set_dump(c, m):
    try:
        chat_id = int(m.command[1])
        config["dump_id"] = chat_id; save_config()
        try: await c.send_message(chat_id, "✅ **Dump Connected!**"); await m.reply_text(f"✅ Dump Set: `{chat_id}`")
        except Exception as e: await m.reply_text(f"⚠️ ID Set, but Bot can't send msg.\nError: `{e}`")
    except: await m.reply_text("Usage: `/setchatid -100xxxxxxx`")

@app.on_message(filters.command("auth") & filters.user(OWNER_ID))
async def auth(c, m):
    try:
        uid = int(m.command[1])
        if uid not in config["auth_users"]: config["auth_users"].append(uid); save_config(); await m.reply_text(f"✅ Authorized: `{uid}`")
    except: pass

@app.on_message(filters.command("unauth") & filters.user(OWNER_ID))
async def unauth(c, m):
    try:
        uid = int(m.command[1])
        if uid in config["auth_users"]: config["auth_users"].remove(uid); save_config(); await m.reply_text(f"🚫 Removed: `{uid}`")
    except: pass

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

if __name__ == "__main__":
    app.start(); app.loop.run_until_complete(web_server()); app.loop.run_forever()
          
