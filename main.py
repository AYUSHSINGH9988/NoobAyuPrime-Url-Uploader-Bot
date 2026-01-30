import os
import time
import aiohttp
import aiofiles
from pyrogram import Client, filters
from aiohttp import web

# --- Environment Variables ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Render के लिए पोर्ट (Default 8080)
PORT = int(os.environ.get("PORT", 8080))

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Web Server (To keep Render Alive) ---
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

# --- Helper Function: Download File ---
async def download_file(url, file_name):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                f = await aiofiles.open(file_name, mode='wb')
                await f.write(await response.read())
                await f.close()
                return file_name
    return None

# --- Bot Commands ---
@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text("Hello! Send me a direct download link, and I will upload it to Telegram.")

@app.on_message(filters.text & filters.private)
async def upload_handler(client, message):
    url = message.text
    if not url.startswith("http"):
        await message.reply_text("Please send a valid URL starting with http/https.")
        return

    msg = await message.reply_text(f"Trying to download...\n`{url}`")
    
    # फाइल का नाम URL से निकालने की कोशिश (Simple logic)
    file_name = url.split("/")[-1]
    if "?" in file_name:
        file_name = file_name.split("?")[0]
    if not "." in file_name:
        file_name = "downloaded_file.bin"

    try:
        # Download
        download_path = await download_file(url, file_name)
        
        if download_path:
            await msg.edit_text("Download complete. Uploading...")
            # Upload
            await message.reply_document(document=download_path)
            await msg.delete()
            # Clean up (Delete file to save space)
            os.remove(download_path)
        else:
            await msg.edit_text("Failed to download. Make sure it's a direct link.")
            
    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")
        if os.path.exists(file_name):
            os.remove(file_name)

# --- Start Bot & Server ---
if __name__ == "__main__":
    print("Starting Bot...")
    app.start()
    # Web server को loop में run करना
    app.loop.run_until_complete(web_server())
    print("Bot is Ready!")
    app.loop.run_forever()
  
