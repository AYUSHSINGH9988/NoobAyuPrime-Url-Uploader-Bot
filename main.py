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
from pyrogram import Client, filters, enums
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

# --- DUMP CHANNEL FIX ---
DUMP_CHANNEL = 0
try:
    dump_id = str(os.environ.get("DUMP_CHANNEL", os.environ.get("LOG_CHANNEL", "0"))).strip()
    if dump_id == "0":
        DUMP_CHANNEL = 0
    elif dump_id.startswith("-100"):
        DUMP_CHANNEL = int(dump_id)
    elif dump_id.startswith("-"):
        DUMP_CHANNEL = int(f"-100{dump_id[1:]}")
    else:
        DUMP_CHANNEL = int(f"-100{dump_id}")
    print(f"✅ Dump Channel Configured: {DUMP_CHANNEL}")
except Exception as e:
    print(f"⚠️ Error parsing DUMP_CHANNEL: {e}")
    DUMP_CHANNEL = 0

PORT = int(os.environ.get("PORT", 8080))

# Initialize Bot
app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, parse_mode=enums.ParseMode.HTML)

# ==========================================
#           GLOBAL VARIABLES
# ==========================================
abort_dict = {} 
user_queues = {}
is_processing = {}
progress_status = {} 
YTDLP_LIMIT = 2000 * 1024 * 1024 

# Objects
mongo_client = None
mongo_db = None
users_col = None
aria2 = None

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
        cmd = ["ffmpeg", "-ss", "00:00:01", "-i", video_path, "-vframes", "1", "-q:v", "2", thumb_path, "-y"]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        if os.path.exists(thumb_path): return thumb_path
    except: pass
    return None

# ==========================================
#           PROGRESS BAR
# ==========================================
async def update_progress_ui(current, total, message, start_time, action, filename="Processing...", queue_pos=None):
    now = time.time()
    last_update = progress_status.get(message.id, 0)
    if (now - last_update < 5) and (current != total): return
    progress_status[message.id] = now
    
    percentage = current * 100 / total if total > 0 else 0
    speed = current / (now - start_time) if (now - start_time) > 0 else 0
    eta = round((total - current) / speed) if speed > 0 else 0
    
    filled = int(percentage // 10)
    bar = '☁️' * filled + '◌' * (10 - filled)
    display_name = urllib.parse.unquote(filename)
    
    text = f"""☁️ <a href='tg://user?id={message.chat.id}'>Powered by Ayuprime</a>

📂 <b>File:</b> {clean_html(display_name)}
"""
    if queue_pos: text += f"🔢 <b>Queue:</b> <code>{queue_pos}</code>\n"
    text += f"""<b>{action}</b>
{bar}  <code>{round(percentage, 1)}%</code>
💾 <b>Size:</b> <code>{humanbytes(current)}</code> / <code>{humanbytes(total)}</code>
🚀 <b>Speed:</b> <code>{humanbytes(speed)}/s</code>
⏳ <b>ETA:</b> <code>{time_formatter(eta)}</code>"""
    
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]])
    try: await message.edit_text(text, reply_markup=buttons)
    except: pass

# ==========================================
#           CORE LOGIC (Extraction)
# ==========================================
def extract_archive(file_path):
    output_dir = f"extracted_{int(time.time())}"
    if not os.path.exists(output_dir): os.makedirs(output_dir)
    if not shutil.which("7z"): return [], None, "7z not installed!"
    cmd = ["7z", "x", file_path, f"-o{output_dir}", "-y"]
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.returncode != 0: return [], output_dir, f"Extraction Error: {process.stderr.decode()}"
    files_list = []
    for root, dirs, files in os.walk(output_dir):
        for file in files: files_list.append(os.path.join(root, file))
    return files_list, output_dir, None

def get_files_from_folder(folder_path):
    files_list = []
    for root, dirs, files in os.walk(folder_path):
        for file in files: files_list.append(os.path.join(root, file))
    return files_list

# ==========================================
#           RCLONE UPLOAD
# ==========================================
async def rclone_upload_file(message, file_path, queue_pos=None):
    file_name = os.path.basename(file_path)
    config_path = "rclone.conf"
    if not os.path.exists(config_path): 
        await message.edit_text("❌ <code>rclone.conf</code> not found!") 
        return False

    display_name = clean_html(file_name)
    await message.edit_text(f"🚀 <b>Starting Rclone Upload...</b>\nFile: {display_name}")
    cmd = ["rclone", "copy", file_path, RCLONE_PATH, "--config", config_path, "-P"]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

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
                text = f"""☁️ <a href='tg://user?id={message.chat.id}'>Powered by Ayuprime</a>
📂 <b>File:</b> {display_name}"""
                if queue_pos: text += f"\n🔢 <b>Queue:</b> <code>{queue_pos}</code>"
                text += f"\n🚀 <b>Rclone Uploading...</b>\n📊 <b>Progress:</b> <code>{match.group(1)}%</code>\n⚡ <b>Status:</b> <code>{clean_html(decoded_line)}</code>"
                try: await message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]]))
                except: pass
                last_update = now

    await process.wait()
    if process.returncode == 0: 
        await message.edit_text(f"✅ <b>Rclone Uploaded!</b>\nFile: {display_name}")
        return True
    else: 
        await message.edit_text(f"❌ <b>Rclone Failed!</b>")
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
        if is_video: thumb_path = await take_screenshot(file_path)
        
        caption = f"☁️ <b>File:</b> {clean_html(file_name)}\n📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(file_path))}</code>\n👤 <b>User:</b> {user_mention}"
        
        target_chat_id = DUMP_CHANNEL if DUMP_CHANNEL != 0 else message.chat.id
        upload_status = "☁️ Uploading to Dump..." if DUMP_CHANNEL != 0 else "☁️ Uploading..."

        try:
            sent_msg = await client.send_document(
                chat_id=target_chat_id, document=file_path, caption=caption, thumb=thumb_path, 
                progress=update_progress_ui, progress_args=(message, time.time(), upload_status, file_name, queue_pos)
            )
            if DUMP_CHANNEL != 0:
                link = f"https://t.me/c/{str(DUMP_CHANNEL)[4:]}/{sent_msg.id}"
                await message.edit_text(f"✅ <b>Uploaded to Dump!</b>\n\n📂 {clean_html(file_name)}\n🔗 <a href='{link}'>View File</a>", disable_web_page_preview=True)
            else: await message.delete()
        except Exception as e:
            await message.edit_text(f"❌ Upload Failed! Error: {e}\nCheck if Bot is Admin in Dump Channel.")
            return False
        
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
        return True
    except Exception as e: 
        print(f"Upload Critical Error: {e}")
        return False

# ==========================================
#           DOWNLOAD LOGIC
# ==========================================
async def download_logic(url, message, user_id, mode, queue_pos=None):
    # --- Pixeldrain Fix ---
    pd_filename = None
    if "pixeldrain.com" in url:
        if "/u/" in url: url = url.replace("/u/", "/api/file/")
        try:
            file_id = url.split("/")[-1]
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://pixeldrain.com/api/file/{file_id}/info") as resp:
                    if resp.status == 200: 
                        data = await resp.json()
                        pd_filename = data.get("name")
        except: pass
    
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        file_path = None
        
        # --- Torrent / Magnet ---
        if url.startswith("magnet:") or url.endswith(".torrent"):
            if not aria2: return "ERROR: Aria2c is not running!"
            try:
                if url.startswith("magnet:"): download = aria2.add_magnet(url)
                else: 
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status != 200: return "ERROR: Torrent File Download Failed"
                            with open("task.torrent", "wb") as f: f.write(await resp.read())
                    download = aria2.add_torrent("task.torrent")
                
                gid = download.gid
                while True:
                    if message.id in abort_dict: 
                        aria2.remove([gid])
                        return "CANCELLED"
                    try:
                        status = aria2.get_download(gid)
                        if status.status == "complete": 
                            file_path = status.files[0].path; break
                        elif status.status == "error": return "ERROR: Aria2 Download Failed"
                        elif status.status == "removed": return "CANCELLED"
                        
                        await update_progress_ui(int(status.completed_length), int(status.total_length), message, time.time(), "☁️ Torrent Downloading...", status.name, queue_pos)
                    except: await asyncio.sleep(2); continue
                    await asyncio.sleep(2)
            except Exception as e: return f"ERROR: Aria2 - {str(e)}"

                # --- YouTube / YT-DLP (720p Limit + Cookies) ---
        elif "youtube.com" in url or "youtu.be" in url or mode == "ytdl":
            try:
                # Check for cookies.txt
                cookie_path = "cookies.txt"
                has_cookies = os.path.exists(cookie_path)
                
                ydl_opts = {
                    # Forces Max 720p resolution
                    'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
                    'outtmpl': '%(title)s.%(ext)s', 
                    'noplaylist': True, 
                    'quiet': True,
                    'merge_output_format': 'mp4',
                    'writethumbnail': True,
                    'cookiefile': cookie_path if has_cookies else None
                }
                
                status_msg = "☁️ Processing YouTube (720p Max)..."
                
                # --- FIX: Message Not Modified Error Fix ---
                try: await message.edit_text(status_msg)
                except: pass
                # -------------------------------------------

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info.get('filesize', 0) > YTDLP_LIMIT: return "ERROR: Video size larger than 2GB Limit"
                    ydl.download([url])
                    file_path = ydl.prepare_filename(info)
            except Exception as e: return f"ERROR: YT-DLP - {str(e)}"
                
                          
                
                status_msg = "☁️ Processing YouTube (720p Max)..."
                await message.edit_text(status_msg)

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info.get('filesize', 0) > YTDLP_LIMIT: return "ERROR: Video size larger than 2GB Limit"
                    ydl.download([url])
                    file_path = ydl.prepare_filename(info)
            except Exception as e: return f"ERROR: YT-DLP - {str(e)}"

        # --- Direct HTTP ---
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
                    file_path = name

                    f = await aiofiles.open(file_path, mode='wb')
                    dl_size = 0
                    start_time = time.time()
                    async for chunk in resp.content.iter_chunked(1024*1024):
                        if message.id in abort_dict: 
                            await f.close(); 
                            if os.path.exists(file_path): os.remove(file_path)
                            return "CANCELLED"
                        await f.write(chunk)
                        dl_size += len(chunk)
                        await update_progress_ui(dl_size, total, message, start_time, "☁️ Downloading...", file_path, queue_pos)
                    await f.close()
        return str(file_path) if file_path else None
    except Exception as e: return f"ERROR: {str(e)}"

# ==========================================
#           PROCESSOR
# ==========================================
async def process_task(client, message, url, mode="auto", upload_target="tg", queue_pos=None):
    try: msg = await message.reply_text("☁️ <b>Initializing Task...</b>")
    except: return

    try:
        file_path = await download_logic(url, msg, message.from_user.id, mode, queue_pos)
        
        if not file_path or str(file_path).startswith("ERROR") or file_path == "CANCELLED":
            await msg.edit_text(f"❌ Failed: {file_path}")
            return

        final_files = []
        is_extracted = False
        
        if os.path.isdir(file_path):
            await msg.edit_text(f"📂 <b>Processing Folder...</b>")
            final_files = get_files_from_folder(file_path)
        elif file_path.lower().endswith((".zip", ".rar", ".7z", ".tar")):
            await msg.edit_text(f"📦 <b>Extracting Archive...</b>")
            extracted_list, temp_dir, error_msg = extract_archive(file_path)
            if error_msg: final_files = [file_path]
            else: 
                final_files = extracted_list
                is_extracted = True
                if os.path.isfile(file_path): os.remove(file_path)
        else: final_files = [file_path]

        if upload_target == "rclone":
             for f in final_files: await rclone_upload_file(msg, f, queue_pos)
        else:
            await msg.edit_text(f"☁️ <b>Uploading {len(final_files)} Files...</b>")
            for index, f in enumerate(final_files):
                if os.path.getsize(f) < 1: continue
                current_status = f"{index+1}/{len(final_files)}"
                await upload_file(client, msg, f, message.from_user.mention, current_status)
                await asyncio.sleep(2) 
        
        if is_extracted: shutil.rmtree(os.path.dirname(final_files[0]), ignore_errors=True)
        elif os.path.isfile(file_path): os.remove(file_path)

        if aria2: aria2.purge() 
    except Exception as e: traceback.print_exc()

# ==========================================
#           QUEUE & COMMANDS
# ==========================================
async def queue_manager(client, user_id):
    if is_processing.get(user_id, False): return
    is_processing[user_id] = True
    while user_queues.get(user_id):
        task = user_queues[user_id].pop(0)
        q_text = f"1/{len(user_queues[user_id])+1}"
        await process_task(client, task[1], task[0], task[2], task[3], q_text)
    is_processing[user_id] = False
    await client.send_message(user_id, "✅ <b>Queue Processed!</b>")

@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    await m.reply_text(f"👋 <b>Ayuprime Bot Ready!</b>\nDump ID: <code>{DUMP_CHANNEL}</code>\n\nCommands:\n/leech [link]\n/rclone [link]\n/ytdl [link]\n/queue [links]")

@app.on_message(filters.command(["leech", "rclone", "queue", "ytdl"]))
async def command_handler(c, m):
    if not m.reply_to_message and len(m.command) < 2: return await m.reply_text("❌ Send a link!")
    text = m.reply_to_message.text if m.reply_to_message else m.text.split(None, 1)[1]
    links = text.split()
    cmd = m.command[0]
    target = "rclone" if cmd == "rclone" else "tg"
    mode = "ytdl" if cmd == "ytdl" else "auto"

    if cmd == "queue":
        if m.from_user.id not in user_queues: user_queues[m.from_user.id] = []
        for l in links: user_queues[m.from_user.id].append((l, m, mode, target))
        await m.reply_text(f"✅ <b>Added {len(links)} Links to Queue!</b>")
        asyncio.create_task(queue_manager(c, m.from_user.id))
    else:
        for l in links: asyncio.create_task(process_task(c, m, l, mode, target))

@app.on_message(filters.text & filters.private)
async def auto_cmd(c, m):
    if not m.text.startswith("/") and ("http" in m.text or "magnet:" in m.text): 
        asyncio.create_task(process_task(c, m, m.text))

@app.on_callback_query(filters.regex(r"cancel_(\d+)"))
async def cancel(c, cb):
    abort_dict[int(cb.data.split("_")[1])] = True
    await cb.answer("🛑 Cancelled!")

# ==========================================
#           MAIN
# ==========================================
async def main():
    print("🤖 Bot Starting...")
    if MONGO_URL:
        try:
            mongo_client = AsyncIOMotorClient(MONGO_URL)
            print("✅ MongoDB Connected!")
        except: print("❌ MongoDB Failed")
            
    global aria2
    try:
        if shutil.which("aria2c"):
            subprocess.Popen(['aria2c', '--enable-rpc', '--rpc-listen-port=6800', '--daemon', '--seed-time=0', '--allow-overwrite=true'])
            await asyncio.sleep(5)
            aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))
            print("✅ Aria2 Connected!")
    except Exception as e: print(f"❌ Aria2 Error: {e}")

    await app.start()
    
    # Check Cookies
    if os.path.exists("cookies.txt"): print("✅ Cookies.txt Found!")
    else: print("⚠️ Cookies.txt NOT Found - YouTube restricted content may fail.")

    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="Bot Running"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
