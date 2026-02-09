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

try:
    DUMP_CHANNEL = int(str(os.environ.get("DUMP_CHANNEL", "0")).strip())
except:
    DUMP_CHANNEL = 0
PORT = int(os.environ.get("PORT", 8080))

# Initialize Bot
app = Client(
    "my_bot", 
    api_id=API_ID, 
    api_hash=API_HASH, 
    bot_token=BOT_TOKEN,
    parse_mode=enums.ParseMode.HTML
)

# ==========================================
#           DATABASE CONNECTION
# ==========================================
if not MONGO_URL:
    print("⚠️ MONGO_URL Missing! Bot will run without Database.")
    mongo_db = None
    users_col = None
else:
    try:
        mongo_client = AsyncIOMotorClient(MONGO_URL)
        mongo_db = mongo_client["URL_Uploader_Bot"]
        users_col = mongo_db["users"]
        print("✅ MongoDB Connected!")
    except Exception as e:
        print(f"❌ MongoDB Error: {e}")
        mongo_db = None

# ==========================================
#           ARIA2 INITIALIZATION
# ==========================================
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
    print("✅ Aria2 Started!")
except Exception as e:
    print(f"❌ Aria2 Error: {e}")

# ==========================================
#           GLOBAL VARIABLES
# ==========================================
abort_dict = {} 
user_queues = {}
is_processing = {}
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

def get_filename_from_header(url, headers):
    try:
        if "Content-Disposition" in headers:
            cd = headers["Content-Disposition"]
            if 'filename="' in cd: return cd.split('filename="')[1].split('"')[0]
            elif "filename=" in cd: return cd.split("filename=")[1].split(";")[0]
    except: pass
    name = url.split("/")[-1].split("?")[0]
    return urllib.parse.unquote(name)

# ==========================================
#           UI PROGRESS
# ==========================================
async def update_progress_ui(current, total, message, start_time, action, filename="Processing...", queue_pos=None):
    now = time.time()
    diff = now - start_time
    if round(diff % 7.00) == 0 or current == total:
        percentage = current * 100 / total if total > 0 else 0
        speed = current / diff if diff > 0 else 0
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

async def rclone_upload_file(message, file_path, queue_pos=None):
    file_name = os.path.basename(file_path)
    config_path = "rclone.conf"
    if not os.path.exists(config_path): await message.edit_text("❌ <code>rclone.conf</code> missing!"); return False

    display_name = clean_html(file_name)
    await message.edit_text(f"🚀 <b>Starting Rclone Upload...</b>\nFile: {display_name}")
    
    cmd = ["rclone", "copy", file_path, RCLONE_PATH, "--config", config_path, "-P"]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    start_time = time.time(); last_update = 0
    while True:
        if message.id in abort_dict: process.kill(); await message.edit_text("❌ Upload Cancelled."); return False
        line = await process.stdout.readline()
        if not line: break
        
        decoded_line = line.decode().strip(); now = time.time()
        if "%" in decoded_line and (now - last_update) > 7:
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

async def upload_file(client, message, file_path, user_mention, queue_pos=None):
    try:
        file_path = str(file_path)
        file_name = os.path.basename(file_path)
        thumb_path = None
        is_video = file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv'))
        if is_video: thumb_path = await take_screenshot(file_path)
        
        caption = f"☁️ <b>File:</b> {clean_html(file_name)}\n📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(file_path))}</code>\n👤 <b>User:</b> {user_mention}"
        
        sent_msg = await message.reply_document(
            document=file_path, 
            caption=caption, 
            thumb=thumb_path, 
            force_document=False, 
            progress=update_progress_ui, 
            progress_args=(message, time.time(), "☁️ Uploading...", file_name, queue_pos)
        )
        
        if DUMP_CHANNEL != 0:
            try:
                file_id = sent_msg.document.file_id if sent_msg.document else sent_msg.video.file_id
                await client.send_document(
                    chat_id=DUMP_CHANNEL, 
                    document=file_id, 
                    caption=caption
                )
            except Exception as e:
                print(f"❌ Dump Failed: {e}")
                try: await sent_msg.copy(DUMP_CHANNEL)
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
    # --- 1. PIXELDRAIN PRE-PROCESSING ---
    pd_filename = None
    if "pixeldrain.com" in url:
        try:
            if "/u/" in url:
                file_id = url.split("pixeldrain.com/u/")[1].split("/")[0]
            else:
                file_id = url.split("/")[-1]
            
            api_url = f"https://pixeldrain.com/api/file/{file_id}/info"
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pd_filename = data.get("name")
                        url = f"https://pixeldrain.com/api/file/{file_id}"
        except Exception as e:
            print(f"Pixeldrain API Error: {e}")

    try:
        file_path = None
        filename_display = "Getting Metadata..."

        # --- 2. TORRENT / MAGNET LOGIC ---
        if url.startswith("magnet:") or url.endswith(".torrent"):
            try:
                if url.endswith(".torrent"):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as resp:
                            if resp.status != 200: return "ERROR: Torrent File Download Failed"
                            torrent_path = f"task_{int(time.time())}.torrent"
                            with open(torrent_path, "wb") as f: f.write(await resp.read())
                    download = aria2.add_torrent(torrent_path)
                else:
                    download = aria2.add_magnet(url)
                
                gid = download.gid
                
                while True:
                    if message.id in abort_dict:
                        aria2.remove([gid])
                        return "CANCELLED"
                        
                    try:
                        status = aria2.tell_status(gid)
                        if status.status == "complete":
                            file_path = status.files[0].path
                            break
                        elif status.status == "error":
                            return "ERROR: Aria2 Download Failed"
                            
                        completed = int(status.completed_length)
                        total = int(status.total_length)
                        if total > 0:
                            await update_progress_ui(completed, total, message, time.time(), "☁️ Torrent Downloading...", status.name, queue_pos)
                            
                    except:
                        await asyncio.sleep(2)
                        continue
                    await asyncio.sleep(2)
                    
            except Exception as e:
                return f"ERROR: Aria2 - {str(e)}"

        # --- 3. YOUTUBE / YT-DLP LOGIC ---
        elif "youtube.com" in url or "youtu.be" in url or mode == "ytdl":
            try:
                ydl_opts = {
                    'format': 'bestvideo+bestaudio/best',
                    'outtmpl': '%(title)s.%(ext)s',
                    'noplaylist': True,
                    'quiet': True,
                    'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    filename_display = info.get('title', 'YouTube Video')
                    
                    if info.get('filesize', 0) > YTDLP_LIMIT:
                        return "ERROR: Video larger than 2GB limit"
                        
                    ydl.download([url])
                    file_path = ydl.prepare_filename(info)
                    
            except Exception as e:
                return f"ERROR: YT-DLP - {str(e)}"

        # --- 4. DIRECT LINK / PIXELDRAIN LOGIC ---
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        total = int(resp.headers.get("content-length", 0))
                        name = pd_filename 
                        
                        if not name:
                            try:
                                if "Content-Disposition" in resp.headers:
                                    cd = resp.headers["Content-Disposition"]
                                    if 'filename="' in cd: name = cd.split('filename="')[1].split('"')[0]
                                    elif "filename=" in cd: name = cd.split("filename=")[1].split(";")[0]
                            except: pass
                        
                        if not name:
                            name = os.path.basename(str(url)).split("?")[0]
                        
                        if not name: name = "downloaded_file"
                        if "." not in name: name += ".mp4"
                        
                        name = urllib.parse.unquote(name)
                        file_path = name
                        filename_display = name

                        f = await aiofiles.open(file_path, mode='wb')
                        dl_size = 0
                        start_time = time.time()
                        last_update = 0
                        
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            if message.id in abort_dict: 
                                await f.close(); os.remove(file_path); return "CANCELLED"
                            
                            await f.write(chunk)
                            dl_size += len(chunk)
                            
                            if (time.time() - last_update > 4) or (dl_size == total):
                                await update_progress_ui(
                                    dl_size, total, message, start_time, 
                                    "☁️ Downloading...", filename_display, queue_pos
                                )
                                last_update = time.time()
                        await f.close()
                    else:
                        return f"ERROR: HTTP {resp.status}"
                        
        return str(file_path) if file_path else None
    except Exception as e: 
        return f"ERROR: {str(e)}"

# ==========================================
#           PROCESSOR & QUEUE
# ==========================================
async def process_task(client, message, url, mode="auto", upload_target="tg", queue_pos=None):
    user_id = message.from_user.id
    try: msg = await message.reply_text("☁️ <b>Initializing...</b>")
    except: return

    try:
        if mongo_db is not None: 
            try: await users_col.update_one({"_id": user_id}, {"$set": {"active": True}}, upsert=True)
            except: pass

        file_path = await download_logic(url, msg, user_id, mode, queue_pos)
        
        if str(file_path).startswith("ERROR"): await msg.edit_text(f"❌ <b>Failed!</b>\nReason: <code>{str(file_path)}</code>"); return
        if file_path == "CANCELLED": await msg.edit_text("❌ Task Cancelled."); return
        if not file_path or not os.path.exists(file_path): await msg.edit_text("❌ Download Failed."); return
        
        file_path = str(file_path); final_files = []; temp_dir = None; is_extracted = False
        
        if os.path.isdir(file_path):
            await msg.edit_text(f"📂 <b>Processing Folder...</b>\n<code>{os.path.basename(file_path)}</code>")
            final_files = get_files_from_folder(file_path)
        elif file_path.lower().endswith((".zip", ".rar", ".7z", ".tar")):
            await msg.edit_text(f"📦 <b>Extracting...</b>\n<code>{os.path.basename(file_path)}</code>")
            extracted_list, temp_dir, error_msg = extract_archive(file_path)
            if error_msg: final_files = [file_path]
            else: final_files = extracted_list; is_extracted = True; os.remove(file_path)
        else: final_files = [file_path]
        
        if not final_files: await msg.edit_text("❌ No files found."); return
        
        if upload_target == "rclone":
            for f in final_files: await rclone_upload_file(msg, f, queue_pos)
        else:
            await msg.edit_text(f"☁️ <b>Uploading {len(final_files)} Files...</b>")
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

        await msg.delete()
        aria2.purge()
        if temp_dir and os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        
    except Exception as e: 
        await msg.edit_text(f"⚠️ Error: <code>{str(e)}</code>")
        traceback.print_exc()

async def queue_manager(client, user_id):
    if is_processing.get(user_id, False): return
    is_processing[user_id] = True
    while user_id in user_queues and user_queues[user_id]:
        task = user_queues[user_id].pop(0); link, message, mode, target = task
        queue_status = f"1/{len(user_queues[user_id]) + 1}"
        await process_task(client, message, link, mode, target, queue_pos=queue_status)
    is_processing[user_id] = False
    await client.send_message(user_id, "✅ <b>Queue Completed!</b>")

# ==========================================
#           COMMANDS
# ==========================================
@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    caption = "<b>👋 Bot Started!</b>\n☁️ <a href='tg://user?id=8493596199'>Powered by Ayuprime</a>\n\n📥 <b>Usage:</b>\n• Send Link -> Leech\n• <code>/rclone link</code> -> Cloud\n• <code>/queue link1 link2</cod 
