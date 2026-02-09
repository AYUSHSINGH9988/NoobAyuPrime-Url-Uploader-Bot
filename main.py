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
from pyrogram import Client, filters, idle, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from motor.motor_asyncio import AsyncIOMotorClient
from aiohttp import web

# ==========================================
#         ENVIRONMENT VARIABLES
# ==========================================
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
RCLONE_PATH = os.environ.get("RCLONE_PATH", "remote:")

# Dump Channel
try:
    dump_id = str(os.environ.get("DUMP_CHANNEL", "0")).strip()
    if dump_id.startswith("-100"): DUMP_CHANNEL = int(dump_id)
    elif dump_id == "0": DUMP_CHANNEL = 0
    else: DUMP_CHANNEL = int(f"-100{dump_id}")
except: DUMP_CHANNEL = 0

PORT = int(os.environ.get("PORT", 8080))

# Initialize Bot
app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, parse_mode=enums.ParseMode.HTML)

# ==========================================
#           DATABASE & ARIA2
# ==========================================
if MONGO_URL:
    try:
        mongo_client = AsyncIOMotorClient(MONGO_URL)
        mongo_db = mongo_client["URL_Uploader_Bot"]
        users_col = mongo_db["users"]
        print("✅ MongoDB Connected!")
    except: mongo_db = None
else: mongo_db = None

# Start Aria2
try:
    cmd = ['aria2c', '--enable-rpc', '--rpc-listen-port=6800', '--daemon', '--seed-time=0', '--max-connection-per-server=10', '--min-split-size=10M', '--follow-torrent=mem', '--allow-overwrite=true']
    subprocess.Popen(cmd)
    time.sleep(1)
    aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))
    print("✅ Aria2 Started!")
except Exception as e: print(f"❌ Aria2 Error: {e}")

# ==========================================
#           GLOBAL VARIABLES
# ==========================================
abort_dict = {} 
user_queues = {}
is_processing = {}
progress_status = {} # Smart Progress Tracking
YTDLP_LIMIT = 2000 * 1024 * 1024

# ==========================================
#           HELPER FUNCTIONS
# ==========================================
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

def clean_html(text):
    if not text: return ""
    return str(text).replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")

async def take_screenshot(video_path):
    try:
        thumb_path = f"{video_path}.jpg"
        cmd = ["ffmpeg", "-ss", "00:00:01", "-i", video_path, "-vframes", "1", "-q:v", "2", thumb_path, "-y"]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        if os.path.exists(thumb_path): return thumb_path
    except: pass
    return None

# ==========================================
#           SMART UI PROGRESS
# ==========================================
async def update_progress_ui(current, total, message, start_time, action, filename="Processing...", queue_pos=None):
    now = time.time()
    # 5 Second Check (Smart Progress)
    last_update = progress_status.get(message.id, 0)
    if (now - last_update < 5) and (current != total): return

    progress_status[message.id] = now
    
    percentage = current * 100 / total if total > 0 else 0
    speed = current / (now - start_time) if (now - start_time) > 0 else 0
    eta = round((total - current) / speed) if speed > 0 else 0
    
    filled = int(percentage // 10)
    bar = '☁️' * filled + '◌' * (10 - filled)
    
    text = f"☁️ <a href='tg://user?id=8493596199'>Powered by Ayuprime</a>\n\n"
    text += f"📂 <b>File:</b> {clean_html(filename)}\n"
    if queue_pos: text += f"🔢 <b>Queue:</b> <code>{queue_pos}</code>\n"
    text += f"<b>{action}</b>\n\n"
    text += f"{bar}  <code>{round(percentage, 1)}%</code>\n\n"
    text += f"💾 <b>Size:</b> <code>{humanbytes(current)}</code> / <code>{humanbytes(total)}</code>\n"
    text += f"🚀 <b>Speed:</b> <code>{humanbytes(speed)}/s</code>\n"
    text += f"⏳ <b>ETA:</b> <code>{time_formatter(eta)}</code>\n"
    
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]])
    try: await message.edit_text(text, reply_markup=buttons)
    except: pass

# ==========================================
#           CORE LOGIC (Extract/Upload)
# ==========================================
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

# --- RCLONE UPLOAD FUNCTION RESTORED ---
async def rclone_upload_file(message, file_path, queue_pos=None):
    file_name = os.path.basename(file_path)
    config_path = "rclone.conf"
    if not os.path.exists(config_path): await message.edit_text("❌ <code>rclone.conf</code> missing!"); return False

    display_name = clean_html(file_name)
    await message.edit_text(f"🚀 <b>Starting Rclone Upload...</b>\nFile: {display_name}")
    
    cmd = ["rclone", "copy", file_path, RCLONE_PATH, "--config", config_path, "-P"]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    last_update = 0
    while True:
        if message.id in abort_dict: process.kill(); await message.edit_text("❌ Upload Cancelled."); return False
        line = await process.stdout.readline()
        if not line: break
        
        decoded_line = line.decode().strip(); now = time.time()
        # Smart Progress for Rclone too (Every 5s)
        if "%" in decoded_line and (now - last_update) > 5:
            match = re.search(r"(\d+)%", decoded_line)
            if match:
                text = f"☁️ <a href='tg://user?id=8493596199'>Powered by Ayuprime</a>\n\n"
                text += f"📂 <b>File:</b> {display_name}\n"
                if queue_pos: text += f"🔢 <b>Queue:</b> <code>{queue_pos}</code>\n"
                text += f"🚀 <b>Rclone Uploading...</b>\n📊 <b>Progress:</b> <code>{match.group(1)}%</code>\n⚡ <b>Status:</b> <code>{clean_html(decoded_line)}</code>"
                try: await message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]])); last_update = now
                except: pass

    await process.wait()
    if process.returncode == 0: await message.edit_text(f"✅ <b>Rclone Uploaded!</b>\nFile: {display_name}"); return True
    else: await message.edit_text(f"❌ <b>Rclone Failed!</b>"); return False

# --- TELEGRAM UPLOAD WITH ANTI-FLOOD ---
async def upload_file(client, message, file_path, user_mention, queue_pos=None):
    try:
        file_path = str(file_path)
        file_name = os.path.basename(file_path)
        thumb_path = None
        is_video = file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv'))
        if is_video: thumb_path = await take_screenshot(file_path)
        
        caption = f"☁️ <b>File:</b> {clean_html(file_name)}\n📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(file_path))}</code>\n👤 <b>User:</b> {user_mention}"
        
        sent_msg = None
        try:
            sent_msg = await message.reply_document(
                document=file_path, caption=caption, thumb=thumb_path, 
                progress=update_progress_ui, 
                progress_args=(message, time.time(), "☁️ Uploading...", file_name, queue_pos)
            )
        except FloodWait as e:
            print(f"⚠️ FloodWait: Sleeping for {e.value} seconds...")
            await asyncio.sleep(e.value + 2)
            sent_msg = await message.reply_document(
                document=file_path, caption=caption, thumb=thumb_path, 
                progress=update_progress_ui, 
                progress_args=(message, time.time(), "☁️ Uploading...", file_name, queue_pos)
            )

        if DUMP_CHANNEL != 0 and sent_msg:
            try: await sent_msg.copy(chat_id=DUMP_CHANNEL, caption=caption)
            except FloodWait as e:
                await asyncio.sleep(e.value)
                await sent_msg.copy(chat_id=DUMP_CHANNEL, caption=caption)
            except: pass
        
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
        return True
    except Exception as e: 
        print(f"Upload Error: {e}")
        return False

# ==========================================
#           DOWNLOAD LOGIC
# ==========================================
async def download_logic(url, message, user_id, mode, queue_pos=None):
    pd_filename = None
    if "pixeldrain.com" in url:
        try:
            file_id = url.split("pixeldrain.com/u/")[1].split("/")[0] if "/u/" in url else url.split("/")[-1]
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://pixeldrain.com/api/file/{file_id}/info") as resp:
                    if resp.status == 200: pd_filename = (await resp.json()).get("name")
                    url = f"https://pixeldrain.com/api/file/{file_id}"
        except: pass

    try:
        file_path = None
        filename_display = "Getting Metadata..."

        if url.startswith("magnet:") or url.endswith(".torrent"):
            try:
                if url.endswith(".torrent"):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as resp:
                            if resp.status != 200: return "ERROR: Torrent Download Failed"
                            with open("task.torrent", "wb") as f: f.write(await resp.read())
                    download = aria2.add_torrent("task.torrent")
                else: download = aria2.add_magnet(url)
                
                gid = download.gid
                while True:
                    if message.id in abort_dict: aria2.remove([gid]); return "CANCELLED"
                    try:
                        status = aria2.tell_status(gid)
                        if status.status == "complete": file_path = status.files[0].path; break
                        elif status.status == "error": return "ERROR: Aria2 Failed"
                        completed = int(status.completed_length); total = int(status.total_length)
                        if total > 0: await update_progress_ui(completed, total, message, time.time(), "☁️ Torrent...", status.name, queue_pos)
                    except: await asyncio.sleep(2); continue
                    await asyncio.sleep(2)
            except Exception as e: return f"ERROR: Aria2 - {str(e)}"

        elif "youtube.com" in url or "youtu.be" in url or mode == "ytdl":
            try:
                ydl_opts = {'format': 'bestvideo+bestaudio/best', 'outtmpl': '%(title)s.%(ext)s', 'noplaylist': True, 'quiet': True, 'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info.get('filesize', 0) > YTDLP_LIMIT: return "ERROR: Video > 2GB"
                    ydl.download([url])
                    file_path = ydl.prepare_filename(info)
            except Exception as e: return f"ERROR: YT-DLP - {str(e)}"

        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200: return f"ERROR: HTTP {resp.status}"
                    total = int(resp.headers.get("content-length", 0))
                    name = pd_filename or os.path.basename(str(url)).split("?")[0]
                    if "." not in name: name += ".mp4"
                    file_path = urllib.parse.unquote(name)

                    f = await aiofiles.open(file_path, mode='wb')
                    dl_size = 0; start_time = time.time()
                    async for chunk in resp.content.iter_chunked(1024*1024):
                        if message.id in abort_dict: await f.close(); os.remove(file_path); return "CANCELLED"
                        await f.write(chunk); dl_size += len(chunk)
                        await update_progress_ui(dl_size, total, message, start_time, "☁️ Downloading...", file_path, queue_pos)
                    await f.close()
        return str(file_path) if file_path else None
    except Exception as e: return f"ERROR: {str(e)}"

# ==========================================
#           PROCESSOR (QUEUE & UPLOAD)
# ==========================================
async def process_task(client, message, url, mode="auto", upload_target="tg", queue_pos=None):
    user_id = message.from_user.id
    try: msg = await message.reply_text("☁️ <b>Initializing...</b>")
    except: return

    try:
        file_path = await download_logic(url, msg, user_id, mode, queue_pos)
        
        if not file_path or str(file_path).startswith("ERROR") or file_path == "CANCELLED":
            await msg.edit_text(f"❌ Failed: {file_path}"); return

        final_files = []
        is_extracted = False
        
        if os.path.isdir(file_path):
            await msg.edit_text(f"📂 <b>Processing Folder...</b>")
            final_files = get_files_from_folder(file_path)
        elif file_path.lower().endswith((".zip", ".rar", ".7z", ".tar")):
            await msg.edit_text(f"📦 <b>Extracting...</b> (Please Wait)")
            extracted_list, temp_dir, error_msg = extract_archive(file_path)
            if error_msg: final_files = [file_path]
            else: final_files = extracted_list; is_extracted = True; os.remove(file_path)
        else: final_files = [file_path]

        # --- UPLOAD SWITCHER ---
        if upload_target == "rclone":
             for f in final_files: await rclone_upload_file(msg, f, queue_pos)
        else:
            await msg.edit_text(f"☁️ <b>Uploading {len(final_files)} Files...</b>")
            for index, f in enumerate(final_files):
                if os.path.getsize(f) < 1024*10: continue
                
                await upload_file(client, msg, f, message.from_user.mention, f"{index+1}/{len(final_files)}")
                await asyncio.sleep(2) # Anti-Flood Sleep
        
        if is_extracted: shutil.rmtree(os.path.dirname(final_files[0]))
        elif os.path.isfile(file_path): os.remove(file_path)

        await msg.delete(); aria2.purge()
    except Exception as e: 
        await msg.edit_text(f"⚠️ Error: {e}"); traceback.print_exc()

async def queue_manager(client, user_id):
    if is_processing.get(user_id, False): return
    is_processing[user_id] = True
    while user_queues.get(user_id):
        task = user_queues[user_id].pop(0)
        # Pass target to processor
        await process_task(client, task[1], task[0], task[2], task[3], f"1/{len(user_queues[user_id])+1}")
    is_processing[user_id] = False
    await client.send_message(user_id, "✅ <b>Queue Completed!</b>")

# ==========================================
#           COMMANDS & SERVER
# ==========================================
@app.on_message(filters.command("start"))
async def start_cmd(c, m): await m.reply_text("👋 <b>Bot Started!</b>\nUse /leech link\nUse /rclone link")

@app.on_message(filters.command(["leech", "rclone", "queue", "ytdl"]))
async def command_handler(c, m):
    if not m.reply_to_message and len(m.command) < 2: await m.reply_text("❌ Send Link!"); return
    text = m.reply_to_message.text if m.reply_to_message else m.text.split(None, 1)[1]
    links = text.split()
    
    cmd = m.command[0]
    target = "rclone" if cmd == "rclone" else "tg"
    mode = "ytdl" if cmd == "ytdl" else "auto"

    if cmd == "queue":
        if m.from_user.id not in user_queues: user_queues[m.from_user.id] = []
        for l in links: user_queues[m.from_user.id].append((l, m, mode, target))
        await m.reply_text("✅ Added to Queue"); asyncio.create_task(queue_manager(c, m.from_user.id))
    else:
        for l in links: asyncio.create_task(process_task(c, m, l, mode, target))

@app.on_message(filters.text & filters.private)
async def auto_cmd(c, m):
    if not m.text.startswith("/") and "http" in m.text: asyncio.create_task(process_task(c, m, m.text))

@app.on_callback_query(filters.regex(r"cancel_(\d+)"))
async def cancel(c, cb): abort_dict[int(cb.data.split("_")[1])] = True; await cb.answer("Cancelling...")

async def main():
    print("🤖 Bot Starting..."); await app.start()
    web_app = web.Application(); web_app.router.add_get("/", lambda r: web.Response(text="Running"))
    runner = web.AppRunner(web_app); await runner.setup(); await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print("✅ Running!"); await idle(); await app.stop()

if __name__ == "__main__": loop = asyncio.get_event_loop(); loop.run_until_complete(main())
  
