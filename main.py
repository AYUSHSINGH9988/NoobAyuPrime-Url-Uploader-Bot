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
from aiohttp import web, ClientPayloadError

# ==========================================
#         ENVIRONMENT VARIABLES
# ==========================================
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
RCLONE_PATH = os.environ.get("RCLONE_PATH", "remote:")

# --- Strict Dump Channel Logic ---
DUMP_CHANNEL = 0
try:
    dump_raw = str(os.environ.get("DUMP_CHANNEL", "0")).strip()
    if dump_raw != "0":
        if dump_raw.startswith("-100"):
            DUMP_CHANNEL = int(dump_raw)
        elif dump_raw.startswith("-"):
            DUMP_CHANNEL = int(f"-100{dump_raw[1:]}")
        else:
            DUMP_CHANNEL = int(f"-100{dump_raw}")
except:
    DUMP_CHANNEL = 0

PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, parse_mode=enums.ParseMode.HTML)

# Globals
abort_dict = {} 
user_queues = {}
is_processing = {}
progress_status = {} 
YTDLP_LIMIT = 2000 * 1024 * 1024 
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

async def update_progress_ui(current, total, message, start_time, action, filename="Processing...", queue_pos=None):
    now = time.time()
    last_update = progress_status.get(message.id, 0)
    if (now - last_update < 5) and (current != total): return
    progress_status[message.id] = now
    
    percentage = current * 100 / total if total > 0 else 0
    speed = current / (now - start_time) if (now - start_time) > 0 else 0
    eta = round((total - current) / speed) if speed > 0 else 0
    
    bar = '☁️' * int(percentage // 10) + '◌' * (10 - int(percentage // 10))
    text = f"📂 <b>File:</b> {clean_html(filename)}\n"
    if queue_pos: text += f"🔢 <b>Queue:</b> <code>{queue_pos}</code>\n"
    text += f"<b>{action}</b>\n{bar} <code>{round(percentage, 1)}%</code>\n💾 <code>{humanbytes(current)} / {humanbytes(total)}</code>\n🚀 <code>{humanbytes(speed)}/s</code> | ⏳ <code>{time_formatter(eta)}</code>"
    
    try: await message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]]))
    except: pass

# ==========================================
#           UPLOAD LOGIC (STRICT DUMP)
# ==========================================
async def upload_file(client, message, file_path, user_mention, queue_pos=None):
    file_name = os.path.basename(file_path)
    thumb_path = await take_screenshot(file_path)
    caption = f"☁️ <b>File:</b> {clean_html(file_name)}\n📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(file_path))}</code>\n👤 <b>User:</b> {user_mention}"
    
    # 1. Check if Dump Channel is Set
    target_chat = DUMP_CHANNEL if DUMP_CHANNEL != 0 else message.chat.id
    status_text = "☁️ Uploading to Dump..." if DUMP_CHANNEL != 0 else "☁️ Uploading..."

    try:
        sent_msg = await client.send_document(
            chat_id=target_chat, 
            document=file_path, 
            caption=caption, 
            thumb=thumb_path, 
            progress=update_progress_ui, 
            progress_args=(message, time.time(), status_text, file_name, queue_pos)
        )
        
        # 2. If uploaded to Dump, notify User in PM (Don't send file again)
        if DUMP_CHANNEL != 0:
            # Create a Link to the message if it's a public channel, otherwise just confirm
            msg_link = f"https://t.me/c/{str(DUMP_CHANNEL)[4:]}/{sent_msg.id}" 
            await message.edit_text(
                f"✅ <b>Successfully Uploaded to Dump!</b>\n\n📂 <b>File:</b> {clean_html(file_name)}\n🔗 <a href='{msg_link}'>View Message</a>",
                disable_web_page_preview=True
            )
        else:
            # If no Dump Channel, file is already in PM
            pass

        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
        return True

    except Exception as e:
        print(f"❌ Upload Error: {e}")
        await message.edit_text(f"⚠️ <b>Upload Failed!</b>\nError: <code>{str(e)}</code>")
        return False

# ==========================================
#           DOWNLOAD LOGIC (WITH RETRY)
# ==========================================
async def download_logic(url, message, user_id, mode, queue_pos=None):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
    
    # --- Retry Wrapper ---
    for attempt in range(3): # Try 3 times
        try:
            file_path = None
            if url.startswith("magnet:") or url.endswith(".torrent"):
                if not aria2: return "ERROR: Aria2c not running!"
                try: aria2.get_global_option()
                except: return "ERROR: Aria2c RPC Connection Failed."

                try:
                    if url.startswith("magnet:"): download = aria2.add_magnet(url)
                    else: 
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url, headers=headers) as resp:
                                with open("task.torrent", "wb") as f: f.write(await resp.read())
                        download = aria2.add_torrent("task.torrent")
                    
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
                            elif status.status == "error": return "ERROR: Aria2 Download Failed"
                            await update_progress_ui(int(status.completed_length), int(status.total_length), message, time.time(), "☁️ Torrent DL...", status.name, queue_pos)
                        except: pass
                        await asyncio.sleep(2)
                except Exception as e: return f"ERROR: Aria2 - {str(e)}"

            elif "youtube.com" in url or "youtu.be" in url or mode == "ytdl":
                try:
                    ydl_opts = {'format': 'bestvideo+bestaudio/best', 'outtmpl': '%(title)s.%(ext)s', 'noplaylist': True, 'quiet': True}
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        file_path = ydl.prepare_filename(info)
                except Exception as e: return f"ERROR: YT-DLP - {str(e)}"

            else:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3600)) as resp:
                        if resp.status != 200: return f"ERROR: HTTP {resp.status}"
                        total = int(resp.headers.get("content-length", 0))
                        name = os.path.basename(str(url)).split("?")[0]
                        if "Content-Disposition" in resp.headers: name = re.findall("filename=(.+)", resp.headers["Content-Disposition"])[0].replace('"', '')
                        if "." not in name: name += ".mp4"
                        file_path = urllib.parse.unquote(name)
                        f = await aiofiles.open(file_path, mode='wb')
                        dl_size = 0
                        start_time = time.time()
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            if message.id in abort_dict: 
                                await f.close()
                                os.remove(file_path)
                                return "CANCELLED"
                            await f.write(chunk)
                            dl_size += len(chunk)
                            await update_progress_ui(dl_size, total, message, start_time, "☁️ Downloading...", file_path, queue_pos)
                        await f.close()
            
            return str(file_path) if file_path else "ERROR: No File Path"

        except (ClientPayloadError, aiohttp.ClientConnectionError, ConnectionResetError) as e:
            print(f"⚠️ Network Error (Attempt {attempt+1}/3): {e}")
            await asyncio.sleep(2) # Wait before retry
            if attempt == 2: return f"ERROR: Network Failed after 3 retries: {e}"
            continue # Retry loop
        except Exception as e:
            return f"ERROR: {str(e)}"

# ==========================================
#           PROCESSOR & RCLONE
# ==========================================
async def rclone_upload_file(message, file_path, queue_pos=None):
    file_name = os.path.basename(file_path)
    config_path = "rclone.conf"
    if not os.path.exists(config_path): 
        await message.edit_text("❌ Config Missing!") 
        return False

    display_name = clean_html(file_name)
    await message.edit_text(f"🚀 <b>Starting Rclone...</b>\n{display_name}")
    
    cmd = ["rclone", "copy", file_path, RCLONE_PATH, "--config", config_path, "-P"]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    last_update = 0
    while True:
        if message.id in abort_dict: 
            process.kill()
            await message.edit_text("❌ Cancelled.")
            return False
        line = await process.stdout.readline()
        if not line: break
        decoded_line = line.decode().strip()
        now = time.time()
        if "%" in decoded_line and (now - last_update) > 5:
            match = re.search(r"(\d+)%", decoded_line)
            if match:
                try: 
                    await message.edit_text(f"🚀 <b>Rclone Uploading...</b>\n<code>{match.group(1)}%</code>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]]))
                    last_update = now
                except: pass
    await process.wait()
    if process.returncode == 0: 
        await message.edit_text(f"✅ <b>Uploaded to Drive!</b>")
        return True
    else: 
        await message.edit_text(f"❌ Rclone Failed.")
        return False

async def process_task(client, message, url, mode="auto", upload_target="tg", queue_pos=None):
    try: msg = await message.reply_text("☁️ <b>Initializing...</b>")
    except: return

    file_path = await download_logic(url, msg, message.from_user.id, mode, queue_pos)
    
    if str(file_path).startswith("ERROR") or file_path == "CANCELLED":
        await msg.edit_text(f"❌ Failed: {file_path}")
        return

    # Direct Upload (No Extraction for simplicity)
    final_files = [file_path] 

    if upload_target == "rclone":
        for f in final_files: await rclone_upload_file(msg, f, queue_pos)
    else:
        await msg.edit_text(f"☁️ <b>Uploading to Dump...</b>")
        for f in final_files: await upload_file(client, msg, f, message.from_user.mention, queue_pos)
    
    if os.path.isfile(file_path): 
        try: os.remove(file_path)
        except: pass

@app.on_message(filters.command(["leech", "rclone", "ytdl"]) & filters.private)
async def command_handler(c, m):
    if len(m.command) < 2: return await m.reply_text("Send Link!")
    link = m.command[1]
    cmd = m.command[0]
    target = "rclone" if cmd == "rclone" else "tg"
    asyncio.create_task(process_task(c, m, link, "ytdl" if "ytdl" in cmd else "auto", target))

async def main():
    global aria2
    print("🤖 Bot Starting...")
    if shutil.which("aria2c"):
        try:
            subprocess.Popen(['aria2c', '--enable-rpc', '--rpc-listen-port=6800', '--daemon', '--allow-overwrite=true'])
            await asyncio.sleep(3)
            aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))
            print("✅ Aria2 Started!")
        except: pass

    await app.start()
    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="Running"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
  
