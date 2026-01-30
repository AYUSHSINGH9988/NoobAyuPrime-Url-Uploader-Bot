import os
import time
import math
import aiohttp
import aiofiles
from urllib.parse import unquote
from pyrogram import Client, filters
from aiohttp import web

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

# Only Bot Client
app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Web Server ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is Running!")
    server = web.Application()
    server.router.add_get("/", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server started on port {PORT}")

# --- Helper functions ---
def humanbytes(size):
    if not size: return ""
    power = 2**10
    n = 0
    Dic_powerN = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + Dic_powerN[n] + 'B'

def time_formatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
        ((str(hours) + "h, ") if hours else "") + \
        ((str(minutes) + "m, ") if minutes else "") + \
        ((str(seconds) + "s") if seconds else "")
    return tmp[:-2] if tmp.endswith(", ") else tmp

async def progress(current, total, message, start_time, action_type):
    now = time.time()
    diff = now - start_time
    if round(diff % 8.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000
        estimated_total_time = elapsed_time + time_to_completion
        
        filled_blocks = int(percentage // 10) 
        empty_blocks = 10 - filled_blocks
        bar = '●' * filled_blocks + '○' * empty_blocks
        
        text = f"**{action_type}**\n" 
        text += f"[{bar}]  `{round(percentage, 2)}%`\n\n"
        text += f"⚡ **Speed:** `{humanbytes(speed)}/s`\n"
        text += f"💾 **Done:** `{humanbytes(current)}` / `{humanbytes(total)}`\n"
        text += f"⏳ **ETA:** `{time_formatter(time_to_completion)}`"
        try:
            await message.edit_text(text)
        except:
            pass

# --- Smart Downloader (Real Filename Logic) ---
async def download_file(url, message, start_time):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                # 1. Header se asli naam nikalna
                file_name = None
                content_disposition = response.headers.get("Content-Disposition")
                if content_disposition:
                    try:
                        file_name = content_disposition.split('filename=')[1].strip('"')
                    except:
                        pass
                
                # 2. Agar header fail ho jaye to URL se nikalna
                if not file_name:
                    file_name = url.split("/")[-1]
                    if "?" in file_name: file_name = file_name.split("?")[0]
                
                # 3. Filename cleaning
                file_name = unquote(file_name)
                if not "." in file_name: file_name += ".mp4"

                total_size = int(response.headers.get("content-length", 0))
                downloaded_size = 0
                
                f = await aiofiles.open(file_name, mode='wb')
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    await f.write(chunk)
                    downloaded_size += len(chunk)
                    if total_size > 0:
                        await progress(downloaded_size, total_size, message, start_time, "📥 Downloading...")
                await f.close()
                return file_name
    return None

# --- Bot Commands ---
@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text("👋 Hello! Send me a direct link (Up to 2GB).")

@app.on_message(filters.text & filters.private)
async def upload_handler(client, message):
    url = message.text
    if not url.startswith("http"):
        return
    msg = await message.reply_text("🔄 **Initializing...**")
    start_time = time.time()
    
    try:
        # Download
        file_path = await download_file(url, msg, start_time)
        
        if file_path:
            await msg.edit_text(f"✅ Downloaded: `{file_path}`\n📤 **Uploading...**")
            up_start_time = time.time()
            
            # Upload
            await message.reply_document(
                document=file_path,
                caption=f"📂 `{file_path}`",
                progress=progress,
                progress_args=(msg, up_start_time, "📤 Uploading...")
            )
            await msg.delete()
            os.remove(file_path)
        else:
            await msg.edit_text("❌ Download Failed! Link might be broken.")
            
    except Exception as e:
        await msg.edit_text(f"⚠️ Error: {str(e)}")
        if 'file_path' in locals() and os.path.exists(file_path):
            os.remove(file_path)

if __name__ == "__main__":
    print("Bot Started...")
    app.start()
    app.loop.run_until_complete(web_server())
    app.loop.run_forever()
    
