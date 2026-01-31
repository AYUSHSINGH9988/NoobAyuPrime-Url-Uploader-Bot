import os
import time
import asyncio
import aiohttp
import aiofiles
import yt_dlp
import aria2p
import subprocess
import shutil
from urllib.parse import unquote
from pyrogram import Client, filters

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
DUMP_ID = int(os.environ.get("DUMP_ID", 0)) # Dump Channel ID
PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Initialize Aria2 (Torrent Engine) ---
# Aria2c ko background mein start kar rahe hain
subprocess.Popen(['aria2c', '--enable-rpc', '--rpc-listen-port=6800', '--daemon'])
time.sleep(1) # Wait for start
aria2 = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))

# --- Limits ---
FREE_LIMIT = 300 * 1024 * 1024
PREM_LIMIT = 1500 * 1024 * 1024 # 1.5GB for Premium
YTDLP_LIMIT = 900 * 1024 * 1024 

# --- Web Server (For Render) ---
from aiohttp import web
async def web_server():
    async def handle(request): return web.Response(text="Ultimate Bot Running!")
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# --- Helper Functions ---
def humanbytes(size):
    if not size: return ""
    power = 2**10
    n = 0
    dic = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power: size /= power; n += 1
    return str(round(size, 2)) + " " + dic[n] + 'B'

async def progress(current, total, message, start_time, action):
    now = time.time()
    diff = now - start_time
    if round(diff % 8.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        text = f"**{action}**\n`{humanbytes(current)}` / `{humanbytes(total)}`\nSpeed: `{humanbytes(speed)}/s`"
        try: await message.edit_text(text)
        except: pass

# --- 1. Hanime & Yt-dlp Downloader ---
async def download_ytdlp(url, message):
    loop = asyncio.get_event_loop()
    def run_dl():
        ydl_opts = {
            'format': 'best',
            'outtmpl': '%(title)s.%(ext)s',
            'max_filesize': YTDLP_LIMIT,
            'quiet': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)
    try:
        await message.edit_text("📥 **Downloading via yt-dlp/Hanime...**")
        return await loop.run_in_executor(None, run_dl)
    except Exception as e:
        print(e)
        return None

# --- 2. Aria2c Torrent Downloader ---
async def download_torrent(link, message):
    try:
        download = aria2.add_magnet(link)
        prev_gid = download.gid
        
        while True:
            download = aria2.get_download(prev_gid)
            if download.status == "complete":
                return download.files[0].path # Return first file path
            elif download.status == "error":
                return None
            
            # Progress update
            if download.total_length > 0:
                await progress(download.completed_length, download.total_length, message, time.time(), "🧲 Leeching Torrent...")
            
            await asyncio.sleep(4)
    except Exception as e:
        print(e)
        return None

# --- 3. Auto-Extraction Logic ---
def extract_zip(file_path):
    output_folder = "extracted_files"
    if os.path.exists(output_folder): shutil.rmtree(output_folder)
    os.makedirs(output_folder)
    
    # Using 7zip (p7zip-full) installed via Docker
    subprocess.run(["7z", "x", file_path, f"-o{output_folder}"], stdout=subprocess.DEVNULL)
    
    files_list = []
    for root, dirs, files in os.walk(output_folder):
        for file in files:
            files_list.append(os.path.join(root, file))
    return files_list

# --- Main Handler ---
@app.on_message(filters.text & filters.private)
async def main_handler(client, message):
    url = message.text
    user_id = message.from_user.id
    
    # Simple Admin Check (For full features)
    is_premium = user_id == OWNER_ID 
    limit = PREM_LIMIT if is_premium else FREE_LIMIT

    msg = await message.reply_text("🔄 **Processing...**")
    file_path = None
    is_zip = False

    try:
        # A. Hanime / YouTube / Social
        if "hanime.tv" in url or "youtube" in url or "youtu.be" in url:
            file_path = await download_ytdlp(url, msg)
        
        # B. Torrent / Magnet
        elif url.startswith("magnet:"):
            file_path = await download_torrent(url, msg)
            
        # C. Direct Link (Existing Logic)
        elif url.startswith("http"):
            # (Shortened for brevity - reuse your old direct download code here or use aiohttp)
            # For now, let's assume it's a direct link handled by aria2 as well!
            # Aria2 handles HTTP links too beautifully.
            try:
                download = aria2.add_uris([url])
                # Wait loop same as torrent...
                while not download.is_complete:
                    download.update()
                    await asyncio.sleep(2)
                    if download.status == 'error': raise Exception("DL Error")
                    await progress(download.completed_length, download.total_length, msg, time.time(), "📥 Downloading...")
                file_path = download.files[0].path
            except:
                await msg.edit_text("❌ Failed. Use Direct Link.")
                return

        # --- Post Download Operations ---
        if file_path and os.path.exists(file_path):
            
            # 1. Extraction Check
            if file_path.endswith(".zip") or file_path.endswith(".rar"):
                await msg.edit_text("📦 **Extracting Archive...**")
                extracted_files = extract_zip(file_path)
                
                if not extracted_files:
                    await msg.edit_text("❌ Archive was empty or password protected.")
                    return

                await msg.edit_text(f"✅ Extracted {len(extracted_files)} files. Uploading...")
                
                for f in extracted_files:
                    # Size Check
                    if os.path.getsize(f) > limit: continue 
                    
                    # Upload Extracted File
                    sent_msg = await message.reply_document(document=f, caption=f"📄 `{os.path.basename(f)}`")
                    
                    # Dump
                    if DUMP_ID != 0:
                        await sent_msg.copy(DUMP_ID)
                    
                    os.remove(f)
                    await asyncio.sleep(2) # Floodwait prevention
                
                await msg.delete()
                
            else:
                # 2. Normal Upload
                await msg.edit_text("📤 **Uploading...**")
                sent_msg = await message.reply_document(
                    document=file_path,
                    caption=f"🎥 `{os.path.basename(file_path)}`",
                    progress=progress,
                    progress_args=(msg, time.time(), "📤 Uploading...")
                )
                
                # 3. Dump Logic
                if DUMP_ID != 0:
                    try:
                        await sent_msg.copy(DUMP_ID)
                        await client.send_message(DUMP_ID, f"User: {message.from_user.mention}\nSource: {url}")
                    except Exception as e:
                        print(f"Dump Error: {e}")

                await msg.delete()

            # Cleanup
            if os.path.exists(file_path): os.remove(file_path)
            # Clean aria2 downloads
            aria2.purge()
            
        else:
            await msg.edit_text("❌ Download Failed!")

    except Exception as e:
        await msg.edit_text(f"⚠️ Error: {str(e)}")

if __name__ == "__main__":
    app.start()
    app.loop.run_until_complete(web_server())
    app.loop.run_forever()
      
