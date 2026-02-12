import os
import time
import asyncio
import aiohttp
import yt_dlp
import aria2p
import shutil
import re
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# ==================== CONFIGURATION ====================
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
RCLONE_PATH = os.environ.get("RCLONE_PATH", "remote:") # Example: "Gdrive:"
PORT = int(os.environ.get("PORT", 8080))

# --- Dump Channel Logic ---
DUMP_CHANNEL = 0
try:
    d = str(os.environ.get("DUMP_CHANNEL", "0")).strip()
    if d != "0":
        if d.startswith("-100"): DUMP_CHANNEL = int(d)
        elif d.startswith("-"): DUMP_CHANNEL = int(f"-100{d[1:]}")
        else: DUMP_CHANNEL = int(f"-100{d}")
except: DUMP_CHANNEL = 0

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Globals ---
aria2 = None
user_queues = {}   # Format: {user_id: [(link, mode, target, message_obj)]}
is_processing = {} # Format: {user_id: True/False}

# ==================== HELPERS ====================
def humanbytes(size):
    if not size: return "0B"
    dic = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    n = 0
    while size > 1024: size /= 1024; n += 1
    return f"{round(size, 2)} {dic[n]}B"

async def progress_bar(current, total, message, start, action, name):
    now = time.time()
    if (now - progress_bar.last < 5) and (current != total): return
    progress_bar.last = now
    pct = current * 100 / total if total else 0
    speed = current / (now - start) if now > start else 0
    
    text = f"📂 <b>{name}</b>\n"
    text += f"<b>{action}</b>\n"
    text += f"☁️ {'☁️' * int(pct // 10)}{'◌' * (10 - int(pct // 10))} <code>{round(pct, 1)}%</code>\n"
    text += f"💾 <code>{humanbytes(current)} / {humanbytes(total)}</code>\n"
    text += f"🚀 <code>{humanbytes(speed)}/s</code>"
    
    try: await message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_{message.id}")]]))
    except: pass
progress_bar.last = 0

async def take_ss(video):
    thumb = f"{video}.jpg"
    try:
        await asyncio.create_subprocess_exec("ffmpeg", "-ss", "00:00:01", "-i", video, "-vframes", "1", "-q:v", "2", thumb, "-y")
        if os.path.exists(thumb): return thumb
    except: pass
    return None

# ==================== UPLOAD LOGIC ====================
async def rclone_upload(message, path):
    name = os.path.basename(path)
    if not os.path.exists("rclone.conf"):
        return await message.edit_text("❌ <b>Error:</b> <code>rclone.conf</code> not found!")
    
    await message.edit_text(f"🚀 <b>Rclone Upload Started...</b>\nTarget: <code>{RCLONE_PATH}</code>")
    
    cmd = ["rclone", "copy", path, RCLONE_PATH, "--config", "rclone.conf", "-P"]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    
    start_time = time.time()
    last_edit = 0
    
    while True:
        line = await proc.stdout.readline()
        if not line: break
        decoded = line.decode().strip()
        
        # Parse Progress from Rclone Output
        if "Transferred" in decoded and "%" in decoded:
            now = time.time()
            if now - last_edit > 5:
                match = re.search(r"(\d+)%", decoded)
                pct = match.group(1) if match else "0"
                try:
                    await message.edit_text(f"🚀 <b>Rclone Uploading...</b>\n📂 {name}\n📊 <b>Progress:</b> <code>{pct}%</code>")
                    last_edit = now
                except: pass
                
    await proc.wait()
    if proc.returncode == 0:
        await message.edit_text(f"✅ <b>Uploaded to Cloud!</b>\n📂 {name}")
    else:
        err = await proc.stderr.read()
        await message.edit_text(f"❌ <b>Rclone Failed!</b>\n<code>{err.decode()[:300]}</code>")

async def telegram_upload(client, message, path):
    name = os.path.basename(path)
    thumb = await take_ss(path)
    caption = f"📂 {name}\n📦 {humanbytes(os.path.getsize(path))}"
    
    target = DUMP_CHANNEL if DUMP_CHANNEL != 0 else message.chat.id
    
    try:
        sent = await client.send_document(
            chat_id=target, document=path, thumb=thumb, caption=caption,
            progress=progress_bar, progress_args=(message, time.time(), "☁️ Uploading...", name)
        )
        if DUMP_CHANNEL != 0:
            link = f"https://t.me/c/{str(DUMP_CHANNEL)[4:]}/{sent.id}"
            await message.edit_text(f"✅ <b>Done!</b>\n<a href='{link}'>View in Channel</a>", disable_web_page_preview=True)
        else:
            await message.edit_text("✅ <b>Upload Complete!</b>")
            
    except Exception as e:
        await message.edit_text(f"❌ <b>Upload Failed!</b>\n<code>{e}</code>")
    
    if thumb and os.path.exists(thumb): os.remove(thumb)

# ==================== PROCESSOR & QUEUE ====================
async def process_task(client, message, link, mode, target):
    try:
        msg = await message.reply_text(f"⏳ <b>Starting Task...</b>\nTarget: {target.upper()}")
        path = None
        
        # --- DOWNLOAD ---
        if "magnet" in link or link.endswith(".torrent"):
            if not aria2: return await msg.edit_text("❌ Aria2 Not Running!")
            if "magnet" in link: dl = aria2.add_magnet(link)
            else:
                async with aiohttp.ClientSession() as s:
                    async with s.get(link) as r:
                        with open("task.torrent", "wb") as f: f.write(await r.read())
                dl = aria2.add_torrent("task.torrent")
            
            gid = dl.gid
            while True:
                s = aria2.tell_status(gid)
                if s.status == "complete": 
                    path = s.files[0].path; break
                elif s.status == "error": 
                    return await msg.edit_text("❌ Torrent Error")
                await asyncio.sleep(3)
                await progress_bar(int(s.completed_length), int(s.total_length), msg, time.time(), "⬇️ Downloading...", s.name)
        else:
            # YTDL / Direct
            opts = {'outtmpl': '%(title)s.%(ext)s', 'quiet': True}
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(link, download=True)
                path = y.prepare_filename(info)
        
        # --- UPLOAD ---
        if path:
            if target == "rclone": await rclone_upload(msg, path)
            else: await telegram_upload(client, msg, path)
            
            if os.path.exists(path): os.remove(path)
            
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

async def queue_manager(client, user_id):
    is_processing[user_id] = True
    while user_id in user_queues and user_queues[user_id]:
        task = user_queues[user_id].pop(0)
        link, mode, target, m = task
        await process_task(client, m, link, mode, target)
        await asyncio.sleep(2) # Cool down
    
    del is_processing[user_id]
    await client.send_message(user_id, "✅ <b>All Queue Tasks Finished!</b>")

# ==================== COMMANDS ====================
@app.on_message(filters.command("start"))
async def start(c, m):
    await m.reply_text("👋 <b>Bot Ready!</b>\n\n/leech [link] - Telegram Upload\n/rclone [link] - Drive Upload\n/queue [link] - Add to Telegram Queue\n/queue_rc [link] - Add to Rclone Queue")

@app.on_message(filters.command(["leech", "rclone", "queue", "queue_rc"]))
async def add_task(c, m):
    if len(m.command) < 2: return await m.reply_text("❌ Give me a link!")
    link = m.command[1]
    cmd = m.command[0]
    
    # Determine Mode & Target
    target = "rclone" if "rclone" in cmd or "rc" in cmd else "telegram"
    mode = "auto"
    is_queue = "queue" in cmd
    
    if is_queue:
        if m.from_user.id not in user_queues: user_queues[m.from_user.id] = []
        user_queues[m.from_user.id].append((link, mode, target, m))
        
        pos = len(user_queues[m.from_user.id])
        await m.reply_text(f"✅ <b>Added to Queue!</b>\nPosition: {pos}\nTarget: {target.upper()}")
        
        if not is_processing.get(m.from_user.id):
            asyncio.create_task(queue_manager(c, m.from_user.id))
    else:
        # Direct Execution
        asyncio.create_task(process_task(c, m, link, mode, target))

# ==================== MAIN ====================
async def main():
    global aria2
    print("🤖 Bot Starting...")
    
    # Rclone Check
    if os.path.exists("rclone.conf"): print("✅ Found rclone.conf")
    else: print("⚠️ Warning: rclone.conf NOT FOUND!")

    # Aria2 Check (Started by start.sh)
    try:
        aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))
        print("✅ Aria2 Connected!")
    except:
        print("❌ Aria2 Failed (Check start.sh)")

    await app.start()
    
    runner = web.AppRunner(web.Application())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
    
