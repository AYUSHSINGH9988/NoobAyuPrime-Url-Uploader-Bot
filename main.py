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
import re
import urllib.parse
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient
from aiohttp import web

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
RCLONE_PATH = os.environ.get("RCLONE_PATH", "remote:")
# Dump Channel ID (e.g., -100xxxx)
DUMP_CHANNEL = int(os.environ.get("DUMP_CHANNEL", 0)) 
PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- MongoDB Setup ---
if not MONGO_URL:
    print("❌ MONGO_URL Missing!")
    mongo_db = None
else:
    mongo_client = AsyncIOMotorClient(MONGO_URL)
    mongo_db = mongo_client["URL_Uploader_Bot"]
    users_col = mongo_db["users"]

# --- Initialize Aria2 ---
try:
    cmd = [
        'aria2c',
        '--enable-rpc',
        '--rpc-listen-port=6800',
        '--daemon',
        '--seed-time=0',
        '--max-connection-per-server=10',
        '--min-split-size=10M',
        '--follow-torrent=mem',
        '--allow-overwrite=true'
    ]
    subprocess.Popen(cmd)
    time.sleep(1)
    aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))
except Exception as e:
    print(f"Aria2 Error: {e}")

# --- Globals ---
abort_dict = {} 
user_queues = {}
is_processing = {}
YTDLP_LIMIT = 2000 * 1024 * 1024

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

def escape_md(text):
    if not text: return ""
    text = str(text)
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)

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
async def update_progress_ui(current, total, message, start_time, action, filename="Processing...", queue_pos=None):
    now = time.time()
    diff = now - start_time
    
    if round(diff % 7.00) == 0 or current == total:
        percentage = current * 100 / total if total > 0 else 0
        speed = current / diff if diff > 0 else 0
        eta = round((total - current) / speed) if speed > 0 else 0
        
        filled = int(percentage // 10)
        bar = '☁️' * filled + '◌' * (10 - filled)
        
        text = f"☁️ [Powered by Ayuprime](tg://user?id=8493596199)\n\n"
        text += f"📂 **File:** `{escape_md(filename)}`\n"
        if queue_pos:
            text += f"🔢 **Queue:** `{queue_pos}`\n"
        text += f"**{action}**\n\n"
        text += f"{bar}  `{round(percentage, 1)}%`\n\n"
        text += f"💾 **Size:** `{humanbytes(current)}` / `{humanbytes(total)}`\n"
        text += f"🚀 **Speed:** `{humanbytes(speed)}/s`\n"
        text += f"⏳ **ETA:** `{time_formatter(eta)}`\n"
        
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]])
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

# --- Rclone Upload ---
async def rclone_upload_file(message, file_path, queue_pos=None):
    file_name = os.path.basename(file_path)
    config_path = "rclone.conf"
    if not os.path.exists(config_path):
         await message.edit_text("❌ `rclone.conf` not found!")
         return False

    safe_name = escape_md(file_name)
    await message.edit_text(f"🚀 **Starting Rclone Upload...**\nFile: `{safe_name}`")
    
    cmd = ["rclone", "copy", file_path, RCLONE_PATH, "--config", config_path, "-P"]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    start_time = time.time(); last_update = 0
    while True:
        if message.id in abort_dict:
            process.kill(); await message.edit_text("❌ Upload Cancelled."); return False

        line = await process.stdout.readline()
        if not line: break
        
        decoded_line = line.decode().strip()
        now = time.time()
        
        if "%" in decoded_line and (now - last_update) > 7:
            match = re.search(r"(\d+)%", decoded_line)
            if match:
                percent = match.group(1)
                text = f"☁️ [Powered by Ayuprime](tg://user?id=8493596199)\n\n"
                text += f"📂 **File:** `{safe_name}`\n"
                if queue_pos: text += f"🔢 **Queue:** `{queue_pos}`\n"
                text += f"🚀 **Rclone Uploading...**\n"
                text += f"📊 **Progress:** `{percent}%`\n"
                text += f"⚡ **Status:** `{escape_md(decoded_line)}`"
                buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]])
                try: await message.edit_text(text, reply_markup=buttons); last_update = now
                except: pass

    await process.wait()
    if process.returncode == 0:
        await message.edit_text(f"✅ **Rclone Uploaded!**\nFile: `{safe_name}`")
        return True
    else:
        await message.edit_text(f"❌ **Rclone Failed!**")
        return False

# --- Telegram Upload Helper ---
async def upload_file(client, message, file_path, user_mention, queue_pos=None):
    try:
        file_path = str(file_path)
        file_name = os.path.basename(file_path)
        thumb_path = None
        is_video = file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv'))
        if is_video: thumb_path = await take_screenshot(file_path)
        
        safe_filename = escape_md(file_name)
        caption = f"☁️ **File:** `{safe_filename}`\n📦 **Size:** `{humanbytes(os.path.getsize(file_path))}`\n👤 **User:** {user_mention}"
        
        sent_msg = await message.reply_document(
            document=file_path, caption=caption, thumb=thumb_path,
            force_document=False, progress=update_progress_ui,
            progress_args=(message, time.time(), "☁️ Uploading...", file_name, queue_pos)
        )
        if DUMP_CHANNEL:
            try: await sent_msg.copy(DUMP_CHANNEL)
            except Exception as e: print(f"Dump Channel Error: {e}")

        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
        return True
    except Exception as e:
        print(f"Upload Error: {e}")
        return False

# --- Get Real Filename ---
def get_filename_from_header(url, headers):
    try:
        if "Content-Disposition" in headers:
            cd = headers["Content-Disposition"]
            if 'filename="' in cd: return cd.split('filename="')[1].split('"')[0]
            elif "filename=" in cd: return cd.split("filename=")[1].split(";")[0]
    except: pass
    name = url.split("/")[-1].split("?")[0]
    return urllib.parse.unquote(name)

# --- Download Logic ---
async def download_logic(url, message, user_id, mode, queue_pos=None):
    if "pixeldrain.com/u/" in url:
        try: url = f"https://pixeldrain.com/api/file/{url.split('pixeldrain.com/u/')[1].split('/')[0]}"
        except: pass

    try:
        file_path = None; filename_display = "Getting Metadata..."

        if mode == "leech" or url.startswith("magnet:") or url.lower().endswith(".torrent"):
            download = None
            if url.startswith("http"):
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(url) as res:
                        if res.status == 200:
                            data = await res.read()
                            meta = f"meta_{time.time()}.torrent"
                            with open(meta, "wb") as f: f.write(data)
                            try: download = aria2.add_torrent(meta)
                            except: return "ERROR: Invalid Torrent."
                        else: return f"ERROR: Link status {res.status}"
            else:
                try: download = aria2.add_magnet(url)
                except: return "ERROR: Invalid Magnet."
            
            if not download: return "ERROR: Failed."
            start_time = time.time()
            while True:
                if message.id in abort_dict: aria2.remove([download]); return "CANCELLED"
                download.update()
                if download.name: filename_display = download.name

                if download.status == "error": return "ERROR: Aria2 Error."
                if download.status == "complete" or (download.total_length > 0 and download.completed_length == download.total_length):
                    file_path = str(download.files[0].path)
                    if os.path.exists(file_path): break
                
                if download.total_length > 0:
                     await update_progress_ui(download.completed_length, download.total_length, message, start_time, "☁️ Leeching...", filename_display, queue_pos)
                await asyncio.sleep(4)

        elif mode == "ytdl" or any(x in url for x in ["youtube", "youtu.be", "hanime", "instagram"]):
             loop = asyncio.get_event_loop()
             def run():
                 opts = {'format': 'best', 'outtmpl': '%(title)s.%(ext)s', 'max_filesize': YTDLP_LIMIT, 'quiet': True, 'nocheckcertificate': True}
                 with yt_dlp.YoutubeDL(opts) as ydl:
                     info = ydl.extract_info(url, download=True)
                     return ydl.prepare_filename(info)
             file_path = await loop.run_in_executor(None, run)
             filename_display = os.path.basename(file_path)

        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        total = int(resp.headers.get("content-length", 0))
                        name = get_filename_from_header(url, resp.headers)
                        if "pixeldrain" in url: name = "pixeldrain_file.mp4"
                        if "." not in name: name += ".mp4"
                        file_path = name; filename_display = name
                        f = await aiofiles.open(file_path, mode='wb')
                        dl_size = 0; start_time = time.time()
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            if message.id in abort_dict: await f.close(); os.remove(file_path); return "CANCELLED"
                            await f.write(chunk); dl_size += len(chunk)
                            await update_progress_ui(dl_size, total, message, start_time, "☁️ Downloading...", filename_display, queue_pos)
                        await f.close()
        return str(file_path) if file_path else None
    except Exception as e: return f"ERROR: {str(e)}"

# --- Main Processor ---
async def process_task(client, message, url, mode="auto", upload_target="tg", queue_pos=None):
    user_id = message.from_user.id
    try: msg = await message.reply_text("☁️ **Initializing...**")
    except: return

    try:
        # FIXED: Database Check
        if mongo_db is not None: 
            await users_col.update_one({"_id": user_id}, {"$set": {"active": True}}, upsert=True)

        file_path = await download_logic(url, msg, user_id, mode, queue_pos)
        
        if str(file_path).startswith("ERROR"): await msg.edit_text(f"❌ **Failed!**\nReason: `{str(file_path)}`"); return
        if file_path == "CANCELLED": await msg.edit_text("❌ Task Cancelled."); return
        if not file_path or not os.path.exists(file_path): await msg.edit_text("❌ Download Failed."); return
        
        file_path = str(file_path); final_files = []; temp_dir = None; is_extracted = False
        if os.path.isdir(file_path):
            await msg.edit_text(f"📂 **Processing Folder...**\n`{os.path.basename(file_path)}`")
            final_files = get_files_from_folder(file_path)
        elif file_path.lower().endswith((".zip", ".rar", ".7z", ".tar")):
            await msg.edit_text(f"📦 **Extracting...**\n`{os.path.basename(file_path)}`")
            extracted_list, temp_dir, error_msg = extract_archive(file_path)
            if error_msg: final_files = [file_path]
            else: final_files = extracted_list; is_extracted = True; os.remove(file_path)
        else: final_files = [file_path]
        
        if not final_files: await msg.edit_text("❌ No files found."); return
        
        if upload_target == "rclone":
            for f in final_files: await rclone_upload_file(msg, f, queue_pos)
        else:
            await msg.edit_text(f"☁️ **Uploading {len(final_files)} Files...**")
            for f in final_files:
                if os.path.getsize(f) < 1024*10: continue
                await upload_file(client, msg, f, message.from_user.mention, queue_pos)
        
        if is_extracted or os.path.isdir(file_path): 
            try: shutil.rmtree(file_path) 
            except: pass
            for f in final_files: 
                try: os.remove(f)
                except: pass
        else:
             try: os.remove(file_path)
             except: pass

        await msg.delete(); aria2.purge()
        if temp_dir and os.path.exists(temp_dir): shutil.rmtree(temp_dir)
    except Exception as e:
        await msg.edit_text(f"⚠️ Error: `{escape_md(str(e))}`"); traceback.print_exc()

# --- Queue Manager ---
async def queue_manager(client, user_id):
    if is_processing.get(user_id, False): return
    is_processing[user_id] = True
    while user_id in user_queues and user_queues[user_id]:
        task = user_queues[user_id].pop(0)
        link, message, mode, target = task
        queue_status = f"1/{len(user_queues[user_id]) + 1}"
        await process_task(client, message, link, mode, target, queue_pos=queue_status)
    is_processing[user_id] = False
    await client.send_message(user_id, "✅ **Queue Completed!**")

# --- Commands ---
@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    caption = "**👋 Bot Started!**\n☁️ [Powered by Ayuprime](tg://user?id=8493596199)\n\n"
    caption += "📥 Send Link -> Instant Leech\n"
    caption += "🚀 **/rclone link** -> Cloud Upload\n"
    caption += "🔢 **/queue link1 link2** -> Queue Process"
    try: await m.reply_photo(photo="start_img.jpg", caption=caption)
    except: await m.reply_text(caption)

@app.on_message(filters.command(["leech", "rclone", "queue", "ytdl"]))
async def command_handler(c, m):
    cmd = m.command[0]; user_id = m.from_user.id
    text = m.reply_to_message.text if m.reply_to_message else (m.text.split(None, 1)[1] if len(m.command) > 1 else "")
    if not text: await m.reply_text("❌ No links found!"); return

    links = text.strip().split(); mode = "ytdl" if cmd == "ytdl" else "auto"
    target = "rclone" if cmd == "rclone" else "tg"
    
    if cmd == "queue":
        if user_id not in user_queues: user_queues[user_id] = []
        for link in links:
            if link.startswith("http") or link.startswith("magnet:"):
                user_queues[user_id].append((link, m, mode, target))
        await m.reply_text(f"✅ **Added {len(links)} links to Queue.**")
        asyncio.create_task(queue_manager(c, user_id))
    else:
        for link in links:
            if link.startswith("http") or link.startswith("magnet:"):
                asyncio.create_task(process_task(c, m, link, mode, target))

@app.on_message(filters.text & filters.private)
async def auto_cmd(c, m):
    if not m.text.startswith("/") and (m.text.startswith("http") or m.text.startswith("magnet:")):
        asyncio.create_task(process_task(c, m, m.text, "auto", "tg"))

@app.on_callback_query(filters.regex(r"cancel_(\d+)"))
async def cancel(c, cb):
    try: abort_dict[int(cb.data.split("_")[1])] = True; await cb.answer("Cancelling Task...")
    except: await cb.answer("Error cancelling.")

# --- Web Server & Main Loop ---
async def web_server():
    async def handle(request): return web.Response(text="Bot Running")
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    # FIXED: Syntax Error removed, arguments added
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

if __name__ == "__main__":
    # FIXED: App start method awaited properly for v2
    app.loop.run_until_complete(app.start())
    app.loop.run_until_complete(web_server())
    app.loop.run_forever()
                       
