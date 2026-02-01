import os
import time
import asyncio
import aiohttp
import aiofiles
import yt_dlp
import aria2p
import subprocess
import shutil
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
    print("❌ MONGO_URL Missing! Data will not be saved permanently.")
    mongo_db = None
else:
    mongo_client = AsyncIOMotorClient(MONGO_URL)
    mongo_db = mongo_client["URL_Uploader_Bot"]
    config_col = mongo_db["config"]
    users_col = mongo_db["users"]

# --- Initialize Aria2 ---
subprocess.Popen(['aria2c', '--enable-rpc', '--rpc-listen-port=6800', '--daemon'])
time.sleep(1)
aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))

# --- Globals ---
abort_dict = {}
YTDLP_LIMIT = 1500 * 1024 * 1024

# --- Database Functions ---
async def is_authorized(user_id):
    if user_id == OWNER_ID: return True
    if mongo_db is None: return False
    user = await users_col.find_one({"_id": user_id})
    return user.get("auth", False) if user else False

async def set_auth(user_id, status: bool):
    if mongo_db is not None:
        await users_col.update_one({"_id": user_id}, {"$set": {"auth": status}}, upsert=True)

async def get_dump_id():
    if mongo_db is None: return 0
    data = await config_col.find_one({"_id": "dump_settings"})
    if not data: return 0
    return data.get("chat_id", 0)

async def set_dump_id(chat_id):
    if mongo_db is not None:
        await config_col.update_one({"_id": "dump_settings"}, {"$set": {"chat_id": chat_id}}, upsert=True)

async def add_user_to_db(user_id):
    if mongo_db is not None:
        await users_col.update_one({"_id": user_id}, {"$set": {"active": True}}, upsert=True)

async def get_all_users():
    if mongo_db is None: return []
    return [doc["_id"] async for doc in users_col.find()]

# --- Web Server ---
from aiohttp import web
async def web_server():
    async def handle(request): return web.Response(text="Bot Running with MongoDB!")
    server = web.Application()
    server.router.add_get("/", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# --- Helper functions (humanbytes, time_formatter, take_screenshot, update_progress_ui etc remain the same) ---
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

async def update_progress_ui(current, total, message, start_time, action):
    now = time.time()
    diff = now - start_time
    if round(diff % 5.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        eta = round((total - current) / speed) if speed > 0 else 0
        filled = int(percentage // 10)
        bar = '☁️' * filled + '◌' * (10 - filled)
        text = f"⚡ [Powered by Ayuprime](tg://user?id=8428298917)\n\n**{action}**\n\n{bar}  `{round(percentage, 1)}%`\n\n💾 **Size:** `{humanbytes(current)}` / `{humanbytes(total)}`\n🚀 **Speed:** `{humanbytes(speed)}/s`\n⏳ **ETA:** `{time_formatter(eta)}`"
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="cancel_task")]])
        try: await message.edit_text(text, reply_markup=buttons)
        except: pass

def extract_archive(file_path):
    output_dir = f"extracted_{int(time.time())}"
    if not os.path.exists(output_dir): os.makedirs(output_dir)
    subprocess.run(["7z", "x", file_path, f"-o{output_dir}", "-y"], stdout=subprocess.DEVNULL)
    files_list = []
    for root, _, files in os.walk(output_dir):
        for file in files: files_list.append(os.path.join(root, file))
    return files_list, output_dir

def get_files_from_folder(folder_path):
    files_list = []
    for root, _, files in os.walk(folder_path):
        for file in files: files_list.append(os.path.join(root, file))
    return files_list

async def upload_file(client, message, file_path, user_mention, dump_id):
    try:
        file_name = os.path.basename(file_path)
        thumb_path = None
        if file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov')):
            thumb_path = await take_screenshot(file_path)
        caption = f"☁️ **File:** `{file_name}`\n📦 **Size:** `{humanbytes(os.path.getsize(file_path))}`\n👤 **User:** {user_mention}"
        sent_msg = await message.reply_document(document=file_path, caption=caption, thumb=thumb_path, force_document=False, progress=update_progress_ui, progress_args=(message, time.time(), "☁️ Uploading..."))
        if dump_id != 0: await sent_msg.copy(dump_id)
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
        return True
    except Exception as e:
        print(f"Upload Error: {e}")
        return False

# --- Download Logic & Process Task (unchanged but wrapped in auth check) ---
async def download_logic(url, message, user_id, mode):
    if "pixeldrain.com/u/" in url:
        try: url = f"https://pixeldrain.com/api/file/{url.split('pixeldrain.com/u/')[1].split('/')[0]}"
        except: pass
    try:
        file_path = None
        if mode == "leech" or url.startswith("magnet:") or url.lower().endswith(".torrent"):
            download = aria2.add_torrent(url) if url.startswith("http") else aria2.add_magnet(url)
            start_time = time.time()
            while True:
                if user_id in abort_dict: aria2.remove([download]); return "CANCELLED"
                download.update()
                if download.status == "complete": 
                    file_path = download.files[0].path; break
                await update_progress_ui(download.completed_length, download.total_length, message, start_time, "☁️ Leeching...")
                await asyncio.sleep(4)
        elif mode == "ytdl":
             def run():
                 with yt_dlp.YoutubeDL({'format': 'best', 'outtmpl': '%(title)s.%(ext)s', 'quiet': True}) as ydl:
                     return ydl.prepare_filename(ydl.extract_info(url, download=True))
             file_path = await asyncio.get_event_loop().run_in_executor(None, run)
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    name = url.split("/")[-1] or "file"
                    file_path = name
                    f = await aiofiles.open(file_path, mode='wb')
                    dl_size = 0; start_time = time.time()
                    async for chunk in resp.content.iter_chunked(1024*1024):
                        if user_id in abort_dict: await f.close(); os.remove(file_path); return "CANCELLED"
                        await f.write(chunk); dl_size += len(chunk)
                        await update_progress_ui(dl_size, int(resp.headers.get("content-length", 0)), message, start_time, "☁️ Downloading...")
                    await f.close()
        return file_path
    except: return None

async def process_task(client, message, url, mode="auto"):
    user_id = message.from_user.id
    if not await is_authorized(user_id):
        return await message.reply_text("🚫 **Access Denied.** Contact the owner.")
    
    await add_user_to_db(user_id)
    if user_id in abort_dict: del abort_dict[user_id]
    msg = await message.reply_text("☁️ **Connecting to Cloud...**")
    file_path = await download_logic(url, msg, user_id, mode)
    if file_path == "CANCELLED": await msg.edit_text("❌ Cancelled."); return
    if not file_path: await msg.edit_text("❌ Download Failed."); return

    final_files = []; temp_dir = None
    if os.path.isdir(file_path): final_files = get_files_from_folder(file_path)
    elif file_path.lower().endswith((".zip", ".rar", ".7z")):
        final_files, temp_dir = extract_archive(file_path)
        os.remove(file_path)
    else: final_files = [file_path]

    dump_id = await get_dump_id()
    for f in final_files:
        await upload_file(client, msg, f, message.from_user.mention, dump_id)
        if temp_dir or os.path.isdir(file_path): os.remove(f)

    await msg.delete()
    if temp_dir: shutil.rmtree(temp_dir)
    aria2.purge()

# --- Updated Command Handlers ---

@app.on_message(filters.command("auth") & filters.user(OWNER_ID))
async def auth_handler(c, m):
    try:
        target_id = int(m.command[1]) if len(m.command) > 1 else (m.reply_to_message.from_user.id if m.reply_to_message else None)
        if not target_id:
            return await m.reply_text("❌ **Usage:** `/auth user_id` or reply to a user.")
        await set_auth(target_id, True)
        await m.reply_text(f"✅ User `{target_id}` is now authorized.")
    except Exception as e: await m.reply_text(f"⚠️ Error: {e}")

@app.on_message(filters.command("unauth") & filters.user(OWNER_ID))
async def unauth_handler(c, m):
    try:
        target_id = int(m.command[1]) if len(m.command) > 1 else (m.reply_to_message.from_user.id if m.reply_to_message else None)
        if not target_id:
            return await m.reply_text("❌ **Usage:** `/unauth user_id` or reply to a user.")
        await set_auth(target_id, False)
        await m.reply_text(f"🚫 User `{target_id}` access revoked.")
    except Exception as e: await m.reply_text(f"⚠️ Error: {e}")

@app.on_message(filters.command("start"))
async def start_cmd(c, m):
    await add_user_to_db(m.from_user.id)
    auth_status = "✅ Authorized" if await is_authorized(m.from_user.id) else "❌ Not Authorized"
    caption = f"**👋 Welcome!**\n\nYour Status: {auth_status}\n\n*Send any link to start!*"
    await m.reply_text(caption)

@app.on_message(filters.command("setchatid") & filters.user(OWNER_ID))
async def set_dump(c, m):
    try:
        chat_id = int(m.command[1])
        await set_dump_id(chat_id)
        await m.reply_text(f"✅ Dump Set: `{chat_id}`")
    except: await m.reply_text("Usage: `/setchatid -100xxxxxxx`")

@app.on_message(filters.text & filters.private)
async def auto_cmd(c, m):
    if not m.text.startswith("/") and (m.text.startswith("http") or m.text.startswith("magnet:")):
        await process_task(c, m, m.text, "auto")

@app.on_callback_query(filters.regex("cancel_task"))
async def cancel(c, cb): abort_dict[cb.from_user.id] = True; await cb.answer("Cancelling...")

if __name__ == "__main__":
    app.start(); app.loop.run_until_complete(web_server()); app.loop.run_forever()
    
