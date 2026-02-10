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

# --- Dump Channel Logic ---
try:
    dump_id = str(os.environ.get("DUMP_CHANNEL", "0")).strip()
    if dump_id == "0":
        DUMP_CHANNEL = 0
    elif dump_id.startswith("-100"):
        DUMP_CHANNEL = int(dump_id)
    elif dump_id.startswith("-"):
        DUMP_CHANNEL = int(f"-100{dump_id[1:]}")
    else:
        DUMP_CHANNEL = int(f"-100{dump_id}")
except Exception as e:
    print(f"⚠️ Error parsing DUMP_CHANNEL: {e}")
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
#           DATABASE & ARIA2 SETUP
# ==========================================
if MONGO_URL:
    try:
        mongo_client = AsyncIOMotorClient(MONGO_URL)
        mongo_db = mongo_client["URL_Uploader_Bot"]
        users_col = mongo_db["users"]
        print("✅ MongoDB Connected Successfully!")
    except Exception as e:
        print(f"❌ MongoDB Error: {e}")
        mongo_db = None
else:
    print("⚠️ MONGO_URL Not Found. Running without Database.")
    mongo_db = None

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
    print("✅ Aria2 Daemon Started Successfully!")
except Exception as e:
    print(f"❌ Aria2 Start Error: {e}")

# ==========================================
#           GLOBAL VARIABLES
# ==========================================
abort_dict = {} 
user_queues = {}
is_processing = {}
progress_status = {}
YTDLP_LIMIT = 2000 * 1024 * 1024 

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
    except Exception as e:
        print(f"Thumbnail Error: {e}")
    return None

# ==========================================
#           SMART PROGRESS BAR
# ==========================================
async def update_progress_ui(current, total, message, start_time, action, filename="Processing...", queue_pos=None):
    now = time.time()
    last_update = progress_status.get(message.id, 0)
    
    # 5 Second Throttle
    if (now - last_update < 5) and (current != total):
        return

    progress_status[message.id] = now
    
    percentage = current * 100 / total if total > 0 else 0
    speed = current / (now - start_time) if (now - start_time) > 0 else 0
    eta = round((total - current) / speed) if speed > 0 else 0
    
    filled = int(percentage // 10)
    bar = '☁️' * filled + '◌' * (10 - filled)
    
    text = f"☁️ <a href='tg://user?id=8493596199'>Powered by Ayuprime</a>\n\n"
    text += f"📂 <b>File:</b> {clean_html(filename)}\n"
    if queue_pos:
        text += f"🔢 <b>Queue:</b> <code>{queue_pos}</code>\n"
    
    text += f"<b>{action}</b>\n\n"
    text += f"{bar}  <code>{round(percentage, 1)}%</code>\n\n"
    text += f"💾 <b>Size:</b> <code>{humanbytes(current)}</code> / <code>{humanbytes(total)}</code>\n"
    text += f"🚀 <b>Speed:</b> <code>{humanbytes(speed)}/s</code>\n"
    text += f"⏳ <b>ETA:</b> <code>{time_formatter(eta)}</code>\n"
    
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]])
    
    try:
        await message.edit_text(text, reply_markup=buttons)
    except Exception:
        pass

# ==========================================
#           CORE LOGIC (Extraction)
# ==========================================
def extract_archive(file_path):
    output_dir = f"extracted_{int(time.time())}"
    if not os.path.exists(output_dir): 
        os.makedirs(output_dir)
        
    if not shutil.which("7z"): 
        return [], None, "7z not installed on server!"

    cmd = ["7z", "x", file_path, f"-o{output_dir}", "-y"]
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    if process.returncode != 0: 
        return [], output_dir, f"Extraction Error: {process.stderr.decode()}"

    files_list = []
    for root, dirs, files in os.walk(output_dir):
        for file in files: 
            files_list.append(os.path.join(root, file))
            
    return files_list, output_dir, None

def get_files_from_folder(folder_path):
    files_list = []
    for root, dirs, files in os.walk(folder_path):
        for file in files: 
            files_list.append(os.path.join(root, file))
    return files_list

# ==========================================
#           RCLONE UPLOAD
# ==========================================
async def rclone_upload_file(message, file_path, queue_pos=None):
    file_name = os.path.basename(file_path)
    config_path = "rclone.conf"
    
    if not os.path.exists(config_path): 
        await message.edit_text("❌ <code>rclone.conf</code> file not found in bot!") 
        return False

    display_name = clean_html(file_name)
    await message.edit_text(f"🚀 <b>Starting Rclone Upload...</b>\nFile: {display_name}")
    
    cmd = ["rclone", "copy", file_path, RCLONE_PATH, "--config", config_path, "-P"]
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
        if not line: 
            break
        
        decoded_line = line.decode().strip()
        now = time.time()
        
        if "%" in decoded_line and (now - last_update) > 5:
            match = re.search(r"(\d+)%", decoded_line)
            if match:
                text = f"☁️ <a href='tg://user?id=8493596199'>Powered by Ayuprime</a>\n\n"
                text += f"📂 <b>File:</b> {display_name}\n"
                if queue_pos: 
                    text += f"🔢 <b>Queue:</b> <code>{queue_pos}</code>\n"
                
                text += f"🚀 <b>Rclone Uploading...</b>\n"
                text += f"📊 <b>Progress:</b> <code>{match.group(1)}%</code>\n"
                text += f"⚡ <b>Status:</b> <code>{clean_html(decoded_line)}</code>"
                
                try: 
                    await message.edit_text(
                        text, 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]])
                    )
                    last_update = now
                except: 
                    pass

    await process.wait()
    
    if process.returncode == 0: 
        await message.edit_text(f"✅ <b>Rclone Uploaded Successfully!</b>\nFile: {display_name}")
        return True
    else: 
        await message.edit_text(f"❌ <b>Rclone Failed!</b>")
        return False

# ==========================================
#           TELEGRAM UPLOAD HELPER
# ==========================================
async def dump_to_channel(client, sent_msg, caption):
    if DUMP_CHANNEL == 0 or not sent_msg:
        return
    try:
        await sent_msg.copy(chat_id=DUMP_CHANNEL, caption=caption)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await sent_msg.copy(chat_id=DUMP_CHANNEL, caption=caption)
    except Exception as e:
        print(f"Dump Copy Error: {e}")
        try:
            await sent_msg.forward(DUMP_CHANNEL)
        except:
            pass

async def upload_file(client, message, file_path, user_mention, queue_pos=None):
    try:
        file_path = str(file_path)
        file_name = os.path.basename(file_path)
        thumb_path = None
        
        is_video = file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv'))
        if is_video: 
            thumb_path = await take_screenshot(file_path)
        
        caption = f"☁️ <b>File:</b> {clean_html(file_name)}\n📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(file_path))}</code>\n👤 <b>User:</b> {user_mention}"
        
        sent_msg = None
        
        try:
            sent_msg = await message.reply_document(
                document=file_path, 
                caption=caption, 
                thumb=thumb_path, 
                progress=update_progress_ui, 
                progress_args=(message, time.time(), "☁️ Uploading...", file_name, queue_pos)
            )
        except FloodWait as e:
            await asyncio.sleep(e.value + 5)
            sent_msg = await message.reply_document(
                document=file_path, 
                caption=caption, 
                thumb=thumb_path, 
                progress=update_progress_ui, 
                progress_args=(message, time.time(), "☁️ Uploading...", file_name, queue_pos)
            )
        except Exception as e:
            print(f"❌ General Upload Error: {e}")
            return False

        await dump_to_channel(client, sent_msg, caption)
        
        if thumb_path and os.path.exists(thumb_path): 
            os.remove(thumb_path)
            
        return True

    except Exception as e: 
        print(f"Upload Critical Error: {e}")
        return False

# ==========================================
#           DOWNLOAD LOGIC (FIXED)
# ==========================================
async def download_logic(url, message, user_id, mode, queue_pos=None):
    # --- Pixeldrain ---
    pd_filename = None
    if "pixeldrain.com" in url:
        try:
            if "/u/" in url:
                file_id = url.split("pixeldrain.com/u/")[1].split("/")[0]
            else:
                file_id = url.split("/")[-1]
                
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://pixeldrain.com/api/file/{file_id}/info") as resp:
                    if resp.status == 200: 
                        data = await resp.json()
                        pd_filename = data.get("name")
                        
            url = f"https://pixeldrain.com/api/file/{file_id}"
        except Exception as e: 
            print(f"Pixeldrain Error: {e}")

    try:
        file_path = None
        
        # --- Torrent ---
        if url.startswith("magnet:") or url.endswith(".torrent"):
            try:
                if url.endswith(".torrent"):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as resp:
                            if resp.status != 200: return "ERROR: Torrent File Download Failed"
                            with open("task.torrent", "wb") as f: f.write(await resp.read())
                    download = aria2.add_torrent("task.torrent")
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
                            await update_progress_ui(
                                completed, total, message, time.time(), 
                                "☁️ Torrent Downloading...", status.name, queue_pos
                            )
                    except: 
                        await asyncio.sleep(2)
                        continue
                    
                    await asyncio.sleep(2)
            except Exception as e: 
                return f"ERROR: Aria2 - {str(e)}"

        # --- YouTube ---
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
                    if info.get('filesize', 0) > YTDLP_LIMIT: 
                        return "ERROR: Video size larger than 2GB Limit"
                        
                    ydl.download([url])
                    file_path = ydl.prepare_filename(info)
            except Exception as e: 
                return f"ERROR: YT-DLP - {str(e)}"

        # --- Direct HTTP (FIXED NAME LOGIC) ---
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200: 
                        return f"ERROR: HTTP {resp.status}"
                        
                    total = int(resp.headers.get("content-length", 0))
                    
                    # 1. Try Pixeldrain Name
                    name = pd_filename
                    
                    # 2. Try Header Name (Content-Disposition) - THIS WAS MISSING
                    if not name:
                        try:
                            if "Content-Disposition" in resp.headers:
                                cd = resp.headers["Content-Disposition"]
                                if 'filename="' in cd:
                                    name = cd.split('filename="')[1].split('"')[0]
                                elif "filename=" in cd:
                                    name = cd.split("filename=")[1].split(";")[0]
                        except:
                            pass
                    
                    # 3. Fallback to URL Name
                    if not name:
                        name = os.path.basename(str(url)).split("?")[0]
                    
                    if "." not in name: name += ".mp4"
                    file_path = urllib.parse.unquote(name)

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
                        
                        await update_progress_ui(
                            dl_size, total, message, start_time, 
                            "☁️ Downloading...", file_path, queue_pos
                        )
                    await f.close()
                    
        return str(file_path) if file_path else None
    except Exception as e: 
        return f"ERROR: {str(e)}"

# ==========================================
#           PROCESSOR (QUEUE & UPLOAD)
# ==========================================
async def process_task(client, message, url, mode="auto", upload_target="tg", queue_pos=None):
    user_id = message.from_user.id
    try: 
        msg = await message.reply_text("☁️ <b>Initializing Task...</b>")
    except: 
        return

    try:
        file_path = await download_logic(url, msg, user_id, mode, queue_pos)
        
        if not file_path or str(file_path).startswith("ERROR") or file_path == "CANCELLED":
            await msg.edit_text(f"❌ Failed: {file_path}")
            return

        final_files = []
        is_extracted = False
        
        if os.path.isdir(file_path):
            await msg.edit_text(f"📂 <b>Processing Folder...</b>")
            final_files = get_files_from_folder(file_path)
        
        elif file_path.lower().endswith((".zip", ".rar", ".7z", ".tar")):
            await msg.edit_text(f"📦 <b>Extracting Archive...</b>\n(This might take some time)")
            extracted_list, temp_dir, error_msg = extract_archive(file_path)
            
            if error_msg: 
                final_files = [file_path]
            else: 
                final_files = extracted_list
                is_extracted = True
                if os.path.isfile(file_path): 
                    try: os.remove(file_path)
                    except: pass
        else: 
            final_files = [file_path]

        if upload_target == "rclone":
             for f in final_files: 
                 await rclone_upload_file(msg, f, queue_pos)
        else:
            await msg.edit_text(f"☁️ <b>Ready to Upload {len(final_files)} Files...</b>")
            
            for index, f in enumerate(final_files):
                if os.path.getsize(f) < 1: continue
                current_status = f"{index+1}/{len(final_files)}"
                await upload_file(client, msg, f, message.from_user.mention, current_status)
                await asyncio.sleep(2) 
        
        if is_extracted: 
            try: shutil.rmtree(os.path.dirname(final_files[0]))
            except: pass
        elif os.path.isfile(file_path): 
            try: os.remove(file_path)
            except: pass

        await msg.delete()
        aria2.purge() 
        
    except Exception as e: 
        try:
            await msg.edit_text(f"⚠️ Process Error: {e}")
        except:
            pass
        traceback.print_exc()

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
        
        q_text = f"1/{len(user_queues[user_id])+1}"
        await process_task(client, msg_obj, link, mode, target, q_text)
        
    is_processing[user_id] = False
    await client.send_message(user_id, "✅ <b>Queue Processed Successfully!</b>")

# ==========================================
#           COMMAND HANDLERS
# ==========================================
@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    welcome_text = """
👋 <b>Welcome to Ayuprime Video Tool Bot!</b>

☁️ <b>I can upload files to Telegram & Cloud (Rclone).</b>

<b>Commands:</b>
• <code>/leech [link]</code> - Upload to Telegram
• <code>/rclone [link]</code> - Upload to Cloud
• <code>/ytdl [link]</code> - Force YouTube-DL mode
• <code>/queue [links]</code> - Add multiple links to queue
    """
    await m.reply_text(welcome_text)

@app.on_message(filters.command(["leech", "rclone", "queue", "ytdl"]))
async def command_handler(c, m):
    if not m.reply_to_message and len(m.command) < 2: 
        await m.reply_text("❌ <b>Please send a link!</b>")
        return
        
    text = m.reply_to_message.text if m.reply_to_message else m.text.split(None, 1)[1]
    links = text.split()
    
    cmd = m.command[0]
    target = "rclone" if cmd == "rclone" else "tg"
    mode = "ytdl" if cmd == "ytdl" else "auto"

    if cmd == "queue":
        if m.from_user.id not in user_queues: 
            user_queues[m.from_user.id] = []
        for l in links: 
            user_queues[m.from_user.id].append((l, m, mode, target))
        await m.reply_text(f"✅ <b>Added {len(links)} Links to Queue!</b>")
        asyncio.create_task(queue_manager(c, m.from_user.id))
    else:
        for l in links: 
            asyncio.create_task(process_task(c, m, l, mode, target))

@app.on_message(filters.text & filters.private)
async def auto_cmd(c, m):
    if not m.text.startswith("/") and ("http" in m.text or "magnet:" in m.text): 
        asyncio.create_task(process_task(c, m, m.text))

@app.on_callback_query(filters.regex(r"cancel_(\d+)"))
async def cancel(c, cb):
    msg_id = int(cb.data.split("_")[1])
    abort_dict[msg_id] = True
    await cb.answer("🛑 Task Cancelled!")

# ==========================================
#           SERVER & STARTUP
# ==========================================
async def main():
    print("🤖 Bot Starting...")
    print(f"📡 DUMP CHANNEL ID: {DUMP_CHANNEL}")
    
    await app.start()
    print("✅ Bot Started Successfully!")

    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="Bot is Running"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"🌍 Web Server running on Port {PORT}")

    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
