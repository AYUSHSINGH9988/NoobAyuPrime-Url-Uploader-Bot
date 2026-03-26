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
import zipfile
import tarfile
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from motor.motor_asyncio import AsyncIOMotorClient
from aiohttp import web

# ==========================================
#         ENVIRONMENT VARIABLES
# ==========================================
API_ID = 33675350
API_HASH = "2f97c845b067a750c9f36fec497acf97"
BOT_TOKEN = "8343193883:AAE738x9dK-c4SdMx0N3HeF8XzrTn3plq8A"
MONGO_URL = "mongodb+srv://gauravsingh576466_db_user:mOuhQVApEQVMpeYr@cluster0.d94qqiv.mongodb.net/BotDatabase?retryWrites=true&w=majority&appName=Cluster0"

RCLONE_PATH = "mega:"

# --- DUMP CHANNEL FIX ---
DUMP_CHANNEL = -1003510428374
print(f"✅ Dump Channel Configured: {DUMP_CHANNEL}")

PORT = int(os.environ.get("PORT", 8080))

app = Client(
    "my_bot", 
    api_id=API_ID, 
    api_hash=API_HASH, 
    bot_token=BOT_TOKEN, 
    parse_mode=enums.ParseMode.HTML
)

# ==========================================
#           GLOBAL VARIABLES
# ==========================================
abort_dict = {} 
user_queues = {}
is_processing = {}
progress_status = {} 
YTDLP_LIMIT = 2000 * 1024 * 1024 

mongo_client = None
mongo_db = None
users_col = None
aria2 = None

# ==========================================
#           PROXY & COOKIE SETTINGS
# ==========================================
PROXY_URL = "http://dLAG1sTQ6:qKE6euVsA@138.249.190.195:62694"
COOKIE_FILE = "cookies.txt"

# ==========================================
#           HELPER FUNCTIONS
# ==========================================
def humanbytes(size):
    if not size: return "0B"
    power = 2**10
    n = 0
    dic = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power: 
        size /= power
        n += 1
    return str(round(size, 2)) + " " + dic[n] + 'B'

def time_formatter(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return "{:02d}:{:02d}:{:02d}".format(int(hours), int(minutes), int(seconds))

def clean_html(text):
    if not text: return ""
    return str(text).replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

async def take_screenshot(video_path):
    try:
        thumb_path = f"{video_path}.jpg"
        cmd = [
            "ffmpeg", "-ss", "00:00:01", 
            "-i", video_path, 
            "-vframes", "1", 
            "-q:v", "2", 
            thumb_path, "-y"
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await process.wait()
        if os.path.exists(thumb_path): 
            return thumb_path
    except Exception:
        pass
    return None

# ==========================================
#           PROGRESS BAR 
# ==========================================
async def update_progress_ui(current, total, message, start_time, action, filename="Processing...", queue_pos=None):
    now = time.time()
    last_update = progress_status.get(message.id, 0)
    
    if (now - last_update < 5) and (current != total):
        return

    progress_status[message.id] = now
    
    percentage = current * 100 / total if total > 0 else 0
    speed = current / (now - start_time) if (now - start_time) > 0 else 0
    eta = round((total - current) / speed) if speed > 0 else 0
    
    filled = int(percentage // 10)
    bar = '☁️' * filled + '◌' * (10 - filled)
    
    display_name = urllib.parse.unquote(filename)
    
    text = f"""☁️ <a href='tg://user?id={message.chat.id}'>Powered by Ayuprime</a>\n\n📂 <b>File:</b> {clean_html(display_name)}\n"""
    if queue_pos: text += f"🔢 <b>Queue:</b> <code>{queue_pos}</code>\n"
    
    text += f"""<b>{action}</b>\n\n{bar}  <code>{round(percentage, 1)}%</code>\n\n💾 <b>Size:</b> <code>{humanbytes(current)}</code> / <code>{humanbytes(total)}</code>\n🚀 <b>Speed:</b> <code>{humanbytes(speed)}/s</code>\n⏳ <b>ETA:</b> <code>{time_formatter(eta)}</code>"""
    
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]])
    try: await message.edit_text(text, reply_markup=buttons)
    except Exception: pass

# ==========================================
#           NATIVE EXTRACTOR
# ==========================================
def extract_archive(file_path):
    output_dir = f"extracted_{int(time.time())}"
    if not os.path.exists(output_dir): os.makedirs(output_dir)
    
    files_list = []
    
    # 1. NATIVE ZIP EXTRACTION
    if file_path.lower().endswith('.zip'):
        try:
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(output_dir)
            for root, dirs, files in os.walk(output_dir):
                for file in files: files_list.append(os.path.join(root, file))
            return files_list, output_dir, None
        except Exception as e:
            return [], output_dir, f"Zip Extraction Error: {e}"
            
    # 2. NATIVE TAR EXTRACTION
    elif file_path.lower().endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tar.xz')):
        try:
            with tarfile.open(file_path, 'r:*') as tar_ref:
                tar_ref.extractall(output_dir)
            for root, dirs, files in os.walk(output_dir):
                for file in files: files_list.append(os.path.join(root, file))
            return files_list, output_dir, None
        except Exception as e:
            return [], output_dir, f"Tar Extraction Error: {e}"

    # 3. FALLBACK TO 7-ZIP
    seven_z_path = shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
    if not seven_z_path: 
        return [], output_dir, "7z not installed on server!"
        
    cmd = [seven_z_path, "x", file_path, f"-o{output_dir}", "-y"]
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.returncode != 0: 
        return [], output_dir, f"Extraction Error: {process.stderr.decode()}"
        
    for root, dirs, files in os.walk(output_dir):
        for file in files: files_list.append(os.path.join(root, file))
    return files_list, output_dir, None

# ==========================================
#           RCLONE UPLOAD 
# ==========================================
async def rclone_upload_file(message, file_path, queue_pos=None):
    file_name = os.path.basename(file_path)
    config_path = "rclone.conf"
    if not os.path.exists(config_path): 
        await message.edit_text("❌ <code>rclone.conf</code> file not found!") 
        return False

    display_name = clean_html(file_name)
    
    cmd = ["rclone", "copy", file_path, RCLONE_PATH, "--config", config_path, "-P", "--ignore-checksum"]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    last_update = 0
    while True:
        if message.id in abort_dict: 
            process.kill()
            await message.edit_text("❌ Upload Cancelled.")
            return False
        line = await process.stdout.readline()
        if not line: break
        decoded_line = line.decode().strip()
        now = time.time()
        
        if "%" in decoded_line and (now - last_update) > 5:
            match = re.search(r"(\d+)%", decoded_line)
            if match:
                text = f"☁️ <a href='tg://user?id={message.chat.id}'>Powered by Ayuprime</a>\n\n📂 <b>File:</b> {display_name}"
                if queue_pos: text += f"\n🔢 <b>Queue:</b> <code>{queue_pos}</code>"
                text += f"\n\n🚀 <b>Rclone Uploading...</b>\n📊 <b>Progress:</b> <code>{match.group(1)}%</code>\n⚡ <b>Status:</b> <code>{clean_html(decoded_line)}</code>"
                try: 
                    await message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]]))
                    last_update = now
                except: pass

    await process.wait()
    if process.returncode == 0: 
        return True
    else: 
        error_msg = await process.stderr.read()
        print(f"Rclone Error: {error_msg.decode()}")
        return False

# ==========================================
#           TELEGRAM UPLOAD 
# ==========================================
async def upload_file(client, message, file_path, user_mention, queue_pos=None):
    try:
        file_path = str(file_path)
        file_name = os.path.basename(file_path)
        thumb_path = None
        
        is_video = file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv'))
        is_image = file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif'))
        
        if is_video: thumb_path = await take_screenshot(file_path)
        
        caption = f"☁️ <b>File:</b> {clean_html(file_name)}\n📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(file_path))}</code>\n👤 <b>User:</b> {user_mention}"
        
        target_chat_id = DUMP_CHANNEL if DUMP_CHANNEL != 0 else message.chat.id
        upload_status = "☁️ Uploading to Dump..." if DUMP_CHANNEL != 0 else "☁️ Uploading..."

        try:
            if is_image:
                sent_msg = await client.send_photo(
                    chat_id=target_chat_id,
                    photo=file_path,
                    caption=caption,
                    progress=update_progress_ui,
                    progress_args=(message, time.time(), upload_status, file_name, queue_pos)
                )
            elif is_video:
                sent_msg = await client.send_video(
                    chat_id=target_chat_id,
                    video=file_path,
                    caption=caption,
                    thumb=thumb_path,
                    supports_streaming=True,
                    progress=update_progress_ui,
                    progress_args=(message, time.time(), upload_status, file_name, queue_pos)
                )
            else:
                sent_msg = await client.send_document(
                    chat_id=target_chat_id,
                    document=file_path, 
                    caption=caption, 
                    thumb=thumb_path, 
                    progress=update_progress_ui, 
                    progress_args=(message, time.time(), upload_status, file_name, queue_pos)
                )

        except Exception as e:
            print(f"❌ Upload Error: {e}")
            return False
        
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
        return True
    except Exception as e: 
        print(f"Upload Critical Error: {e}")
        return False

# ==========================================
#           DOWNLOAD LOGIC
# ==========================================
async def download_logic(url, message, user_id, mode, queue_pos=None, custom_name=None):
    TRACKERS = [
        "http://tracker.opentrackr.org:1337/announce",
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://tracker.openbittorrent.com:80/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://explodie.org:6969/announce",
        "udp://tracker.doko.moe:6969/announce",
        "http://tracker.openbittorrent.com:80/announce",
        "udp://open.demonii.com:1337/announce",
        "udp://tracker.coppersurfer.tk:6969/announce",
        "udp://tracker.leechers-paradise.org:6969/announce",
    ]
    tracker_str = ",".join(TRACKERS)

    pd_filename = None
    if "pixeldrain.com" in url:
        try:
            file_id = url.split("pixeldrain.com/u/")[1].split("/")[0] if "/u/" in url else url.split("/")[-1]
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://pixeldrain.com/api/file/{file_id}/info") as resp:
                    if resp.status == 200: 
                        data = await resp.json()
                        pd_filename = data.get("name")
            url = f"https://pixeldrain.com/api/file/{file_id}"
        except Exception as e: print(f"Pixeldrain Info Error: {e}")
    
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        file_path = None
        
        # 1. --- Torrent / Magnet ---
        if url.startswith("magnet:") or url.endswith(".torrent"):
            if not aria2: return "ERROR: Aria2c is not running!"
            
            try:
                options = {'bt-tracker': tracker_str}
                if custom_name and not url.startswith("magnet:"): 
                    options['out'] = custom_name
                    
                if url.startswith("magnet:"):
                    download = aria2.add_magnet(url, options=options)
                else: 
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status != 200: return "ERROR: Torrent File Download Failed"
                            with open("task.torrent", "wb") as f: f.write(await resp.read())
                    download = aria2.add_torrent("task.torrent", options=options)
                
                gid = download.gid
                
                while True:
                    if message.id in abort_dict: 
                        aria2.remove([gid])
                        return "CANCELLED"
                        
                    try:
                        status = aria2.get_download(gid)
                        
                        if status.status == "complete": 
                            if len(status.files) > 1:
                                base_dir = str(status.dir)
                                rel_path = os.path.relpath(str(status.files[0].path), base_dir)
                                file_path = os.path.join(base_dir, rel_path.split(os.sep)[0])
                            else:
                                file_path = str(status.files[0].path)
                            break
                        elif status.status == "error": 
                            return "ERROR: Aria2 Download Failed"
                        elif status.status == "removed":
                            return "CANCELLED"
                        
                        if status.total_length > 0 and status.completed_length >= status.total_length:
                             if len(status.files) > 1:
                                 base_dir = str(status.dir)
                                 rel_path = os.path.relpath(str(status.files[0].path), base_dir)
                                 file_path = os.path.join(base_dir, rel_path.split(os.sep)[0])
                             else:
                                 file_path = str(status.files[0].path)
                             break
                        
                        await update_progress_ui(
                            int(status.completed_length), int(status.total_length), message, time.time(), 
                            f"☁️ Downloading ({status.num_seeders} Seeds)...", status.name, queue_pos
                        )
                    except Exception as e: 
                        print(f"Aria2 Stats Error: {e}")
                        await asyncio.sleep(2)
                        continue
                    
                    await asyncio.sleep(2)
            except Exception as e: 
                return f"ERROR: Aria2 - {str(e)}"

        # 2. --- YouTube / YT-DLP (HARDCORE ANTI-BOT & JS SOLVER ENABLED) ---
        elif "youtube.com" in url or "youtu.be" in url or mode == "ytdl" or "m3u8" in url:
            try:
                out_name = custom_name if custom_name else '%(title)s.%(ext)s'
                
                ydl_opts = {
                    'format': 'bestvideo+bestaudio/best', 
                    'merge_output_format': 'mp4',         
                    'outtmpl': out_name, 
                    'noplaylist': True, 
                    'quiet': True,
                    'cookiefile': COOKIE_FILE,          
                    'proxy': PROXY_URL,                 
                    'geo_bypass': True,
                    'nocheckcertificate': True,
                    'sleep_requests': 1,                
                    'remote_components': ['ejs:github'],
                    'extractor_args': {
                        'youtube': ['player_client=android,web,tv']
                    },
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-us,en;q=0.5',
                        'Sec-Fetch-Mode': 'navigate'
                    }
                }
                
                def run_ytdl():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        if info.get('filesize', 0) > YTDLP_LIMIT: 
                            return "ERROR: Video size larger than 2GB Limit"
                        ydl.download([url])
                        return ydl.prepare_filename(info)

                file_path = await asyncio.to_thread(run_ytdl)
                
                if isinstance(file_path, str) and file_path.startswith("ERROR"):
                    return file_path

                if custom_name and os.path.exists(custom_name):
                    file_path = custom_name

            except Exception as e: 
                return f"ERROR: YT-DLP - {str(e)}"

        # 3. --- Direct HTTP ---
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200: return f"ERROR: HTTP {resp.status}"
                    total = int(resp.headers.get("content-length", 0))
                    
                    name = pd_filename
                    if not name and "Content-Disposition" in resp.headers:
                        try:
                            cd = resp.headers["Content-Disposition"]
                            if 'filename="' in cd: name = cd.split('filename="')[1].split('"')[0]
                            elif "filename=" in cd: name = cd.split("filename=")[1].split(";")[0]
                        except: pass
                    
                    if not name: name = os.path.basename(str(url)).split("?")[0]
                    name = urllib.parse.unquote(name)
                    if "." not in name: name += ".mp4"
                    
                    file_path = custom_name if custom_name else name
                    f = await aiofiles.open(file_path, mode='wb')
                    dl_size = 0
                    start_time = time.time()
                    
                    async for chunk in resp.content.iter_chunked(1024*1024):
                        if message.id in abort_dict: 
                            await f.close()
                            if os.path.exists(file_path): os.remove(file_path)
                            return "CANCELLED"
                        await f.write(chunk)
                        dl_size += len(chunk)
                        await update_progress_ui(dl_size, total, message, start_time, "☁️ Downloading...", file_path, queue_pos)
                    await f.close()
                    
        return str(file_path) if file_path else None
    except Exception as e: 
        return f"ERROR: {str(e)}"
                                         
# ==========================================
#           PROCESSOR
# ==========================================
async def process_task(client, message, url, mode="auto", upload_target="tg", queue_pos=None, custom_name=None):
    try: msg = await message.reply_text("☁️ <b>Initializing Task...</b>")
    except: return

    try:
        file_path = None
        
        if not url and message.reply_to_message:
            media = message.reply_to_message.document or message.reply_to_message.video or message.reply_to_message.photo
            fname = custom_name if custom_name else getattr(media, 'file_name', None)
            if not fname: fname = f"tg_file_{int(time.time())}"
            if not os.path.exists("downloads"): os.makedirs("downloads")
            file_path = os.path.join("downloads", fname)
            await msg.edit_text(f"📥 <b>Downloading from TG...</b>\n<code>{clean_html(fname)}</code>")
            file_path = await message.reply_to_message.download(
                file_name=file_path, 
                progress=update_progress_ui, 
                progress_args=(msg, time.time(), "📥 Downloading...", fname, queue_pos)
            )
            if not file_path:
                await msg.edit_text("❌ TG Download Failed!")
                return
        elif url:
            file_path = await download_logic(url, msg, message.from_user.id, mode, queue_pos, custom_name)
        
        if not file_path or str(file_path).startswith("ERROR") or file_path == "CANCELLED":
            await msg.edit_text(f"❌ Failed: {file_path}")
            return

        final_files = []
        is_extracted = False
        file_path_str = str(file_path)
        original_name = os.path.basename(file_path_str)
        is_archive = False

        if os.path.isfile(file_path_str):
            if file_path_str.lower().endswith(('.zip', '.rar', '.7z', '.tar', '.gz', '.iso', '.xz')):
                is_archive = True
            elif re.search(r'\.\d{3}$', file_path_str): 
                is_archive = True
            else:
                try:
                    mime = subprocess.check_output(['file', '--mime-type', '-b', file_path_str]).decode().strip()
                    if "zip" in mime or "archive" in mime: is_archive = True
                except: pass

        if is_archive:
            await msg.edit_text(f"📦 <b>Extracting Archive...</b>")
            extracted_list, temp_dir, error_msg = extract_archive(file_path_str)
            if not error_msg and extracted_list:
                try: extracted_list.sort(key=natural_sort_key)
                except: extracted_list.sort()
                final_files = extracted_list
                is_extracted = True
                if os.path.isfile(file_path_str): os.remove(file_path_str)
            else:
                final_files = [file_path_str]
        elif os.path.isdir(file_path_str):
            folder_files = []
            for root, dirs, files in os.walk(file_path_str):
                for f in files: folder_files.append(os.path.join(root, f))
            try: folder_files.sort(key=natural_sort_key)
            except: folder_files.sort()
            final_files = folder_files
        else:
            final_files = [file_path_str]

        if DUMP_CHANNEL != 0:
            try:
                pin_title = os.path.splitext(original_name)[0]
                pin_msg = await client.send_message(DUMP_CHANNEL, f"📌 <b>Batch Upload:</b>\n<code>{clean_html(pin_title)}</code>")
                await pin_msg.pin(both_sides=True)
            except: pass

        if upload_target == "rclone":
            for f in final_files: await rclone_upload_file(msg, f, queue_pos)
        else:
            await msg.edit_text(f"☁️ <b>Uploading {len(final_files)} Files...</b>")
            for index, f in enumerate(final_files):
                if message.id in abort_dict: break
                current_status = f"{index+1}/{len(final_files)}"
                uploaded = await upload_file(client, msg, f, message.from_user.mention, current_status)
                if not uploaded:
                    await asyncio.sleep(2)
                    await upload_file(client, msg, f, message.from_user.mention, current_status)
                await asyncio.sleep(2) 
        
        if is_extracted: shutil.rmtree(temp_dir, ignore_errors=True)
        elif os.path.isdir(file_path_str): shutil.rmtree(file_path_str, ignore_errors=True)
        elif os.path.isfile(file_path_str) and file_path_str not in final_files: os.remove(file_path_str)
        for f in final_files:
            if os.path.exists(f): 
                try: os.remove(f)
                except: pass
        
        if message.id not in abort_dict:
            await msg.edit_text("✅ <b>Task Completed!</b>")
            
    except Exception as e:
        traceback.print_exc()
        await msg.edit_text(f"⚠️ Error: {e}")
                                
# ==========================================
#           QUEUE MANAGER
# ==========================================
async def queue_manager(client, user_id):
    if is_processing.get(user_id, False): 
        return
        
    is_processing[user_id] = True
    
    while user_queues.get(user_id):
        task = user_queues[user_id].pop(0)
        link = task[0]
        msg_obj = task[1]
        mode = task[2]
        target = task[3]
        custom_name = task[4]
        
        q_text = f"1/{len(user_queues[user_id])+1}"
        await process_task(client, msg_obj, link, mode, target, q_text, custom_name)
        
    is_processing[user_id] = False
    await client.send_message(user_id, "✅ <b>Queue Processed Successfully!</b>")

# ==========================================
#           COMMAND HANDLERS
# ==========================================
@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    welcome_text = f"""
👋 <b>Welcome to Ayuprime Video Tool Bot!</b>

☁️ <b>I can upload files to Telegram & Cloud (Rclone).</b>
📡 <b>Dump Channel:</b> <code>{DUMP_CHANNEL}</code>

<b>Commands:</b>
• <code>/leech [link]</code> - Upload to Telegram
• <code>/rclone [link]</code> - Upload to Cloud
• <code>/ytdl [link]</code> - Force YouTube-DL mode
• <code>/queue [links]</code> - Add multiple links to queue

📝 <b>Custom Rename Example:</b>
<code>/ytdl https://link.m3u8 | My Video Name.mp4</code>
    """
    await m.reply_text(welcome_text)

@app.on_message(filters.command(["leech", "rclone", "queue", "ytdl"]))
async def command_handler(c, m):
    if not m.reply_to_message and len(m.command) < 2: 
        await m.reply_text("❌ <b>Please send a link!</b>")
        return
        
    text = m.reply_to_message.text if m.reply_to_message else m.text.split(None, 1)[1]
    
    custom_name = None
    if "|" in text:
        parts = text.split("|", 1)
        text = parts[0].strip()
        custom_name = parts[1].strip()
        
    links = text.split()
    
    cmd = m.command[0]
    target = "rclone" if cmd == "rclone" else "tg"
    mode = "ytdl" if cmd == "ytdl" else "auto"

    if cmd == "queue":
        if m.from_user.id not in user_queues: 
            user_queues[m.from_user.id] = []
        for l in links: 
            user_queues[m.from_user.id].append((l, m, mode, target, custom_name))
        await m.reply_text(f"✅ <b>Added {len(links)} Links to Queue!</b>")
        asyncio.create_task(queue_manager(c, m.from_user.id))
    else:
        for l in links: 
            asyncio.create_task(process_task(c, m, l, mode, target, None, custom_name))

@app.on_message(filters.text & filters.private)
async def auto_cmd(c, m):
    if not m.text.startswith("/") and ("http" in m.text or "magnet:" in m.text): 
        text = m.text
        custom_name = None
        if "|" in text:
            parts = text.split("|", 1)
            text = parts[0].strip()
            custom_name = parts[1].strip()
            
        asyncio.create_task(process_task(c, m, text.split()[0], custom_name=custom_name))

@app.on_callback_query(filters.regex(r"cancel_(\d+)"))
async def cancel(c, cb):
    msg_id = int(cb.data.split("_")[1])
    abort_dict[msg_id] = True
    await cb.answer("🛑 Task Cancelled!")
# ==========================================
#        AUTO CLEANER ROBOT 🧹
# ==========================================
async def auto_cleaner():
    """Background task jo har 5 minute mein server se kachra saaf karega"""
    while True:
        await asyncio.sleep(300) # 5 minutes
        try:
            # Root aur downloads folder dono mein safaai karega
            os.system("rm -rf *.torrent *.part *.ytdl task.torrent extracted_* downloads/*.torrent downloads/*.part")
        except Exception:
            pass

# ==========================================
#           MAIN RUNNER
# ==========================================
async def main():
    print("🤖 Bot Starting...")
    global aria2

    try:
        subprocess.run(["pkill", "-9", "aria2c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass

    if shutil.which("aria2c"):
        try:
            cmd = [
                'aria2c', 
                '--enable-rpc', 
                '--rpc-listen-port=6800', 
                '--rpc-secret=my_secret_token',
                '--daemon', 
                '--allow-overwrite=true', 
                '--max-connection-per-server=10',
                '--seed-time=0',
                '--seed-ratio=0.0',
                '--follow-torrent=mem'
            ]
            subprocess.Popen(cmd)
            print("⏳ Starting Aria2c...")
            await asyncio.sleep(4) 
            
            aria2 = aria2p.API(aria2p.Client(
                host="http://localhost", 
                port=6800, 
                secret="my_secret_token"
            ))
            print("✅ Aria2 Connected Successfully!")
        except Exception as e:
            print(f"❌ Aria2 Start Failed: {e}")
            aria2 = None
    else:
        print("❌ Error: 'aria2c' binary not found. Install it (apt install aria2)")

    await app.start()
    
    # KACHRA SAFAAI WALE ROBOT KO BHI START KAR DIYA
    asyncio.create_task(auto_cleaner())
    
    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="Bot Running"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"🌍 Web Server running on Port {PORT}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())