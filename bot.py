import os
import mmap
import asyncio
from pathlib import Path
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, FSInputFile
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import aiohttp

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Error: BOT_TOKEN environment variable is missing.")

LOCAL_DATA_DIR = "/app/data" 
os.makedirs(LOCAL_DATA_DIR, exist_ok=True)

# 2GB Limit for Local API
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024  
THRESHOLD_FOR_TEXT_OUTPUT = 20 

router = Router()
dp = Dispatcher()
dp.include_router(router)

# ================= CORE LOGIC =================

def parse_line(line: str):
    line = line.strip()
    if not line: return "", "", ""
    
    c1 = line.find(":")
    if c1 == -1: return line, "", ""
    
    url_end = c1
    if len(line) > c1 + 2 and line[c1:c1+3] == "://":
        c2 = line.find(":", c1 + 3)
        if c2 != -1: url_end = c2
        
    url_s = line[:url_end]
    rest = line[url_end + 1:]
    sep = rest.find(":")
    if sep == -1: return url_s, rest, ""
    
    return url_s, rest[:sep], rest[sep+1:]

def search_file_sync(filepath: str, keyword: str) -> list:
    results = []
    try:
        if os.path.getsize(filepath) == 0: return results
        with open(filepath, "rb") as fh:
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                sz = mm.size()
                pos = 0
                kw_lower = keyword.lower()
                while pos < sz:
                    end = mm.find(b"\n", pos)
                    if end == -1: end = sz
                    raw = mm[pos:end]
                    pos = end + 1
                    if raw.endswith(b"\r"): raw = raw[:-1]
                    if not raw: continue
                    
                    decoded = raw.decode("utf-8", errors="ignore").strip()
                    u, l, p = parse_line(decoded)
                    
                    if kw_lower in u.lower():
                        if l and p:
                            results.append(f"{l}:{p}")
    except Exception as e:
        print(f"Scan error in {filepath}: {e}")
    return results

async def perform_search(keyword: str) -> list:
    all_hits = set()
    folder_path = Path(LOCAL_DATA_DIR)
    
    if not folder_path.exists():
        return []
        
    files = [f for f in folder_path.glob("*") if f.suffix.lower() == ".txt"]
    if not files:
        return []

    tasks = []
    for fp in files:
        task = asyncio.create_task(
            asyncio.to_thread(search_file_sync, str(fp), keyword)
        )
        tasks.append(task)
    
    for future in asyncio.as_completed(tasks):
        res = await future
        all_hits.update(res)
        
    return sorted(list(all_hits))

# ================= LOCAL API HELPERS =================

async def get_file_from_local_api(bot: Bot, file_id: str) -> str:
    """Calls Local Bot API getFile and returns the local download URL."""
    url = f"http://localhost:8081/bot{bot.token}/getFile"
    payload = {"file_id": file_id}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if "result" in data:
                file_path = data["result"]["file_path"]
                # Construct full local URL
                return f"http://localhost:8081/file/{bot.token}/{file_path}"
            else:
                raise Exception(f"API Error: {data}")

async def download_file_from_local_api(url: str, dest_path: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                with open(dest_path, 'wb') as f:
                    f.write(await resp.read())
            else:
                raise Exception(f"Download failed: {resp.status}")

# ================= HANDLERS =================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎨 *ILPicasso Scanner (Local API Edition)*\n\n"
        "**Limits Raised:** Supports files up to 2GB.\n\n"
        "**Workflow:**\n"
        "1. Send `.txt` file.\n"
        "2. Wait for download confirmation.\n"
        "3. Use `/u <domain>` to search.",
        parse_mode=ParseMode.MARKDOWN
    )

# FIX: Using explicit filter definition instead of lambda/regexp directly in decorator
@router.message(F.document & F.document.file_name.regexp(r".*\.txt$"))
async def handle_document(message: Message):
    doc = message.document
    
    if doc.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(f"❌ File too large! Max allowed is 2GB.\nYour file: {doc.file_size / (1024*1024*1024):.2f} GB")
        return

    status_msg = await message.reply_text("📥 Contacting Local API...")
    
    try:
        # 1. Get local URL from our custom API server
        local_download_url = await get_file_from_local_api(message.bot, doc.file_id)
        
        # 2. Define save path
        safe_name = "".join(c for c in doc.file_name if c.isalnum() or c in ('.', '_', '-')).rstrip()
        if not safe_name: 
            safe_name = f"uploaded_{doc.file_unique_id}.txt"
        
        target_path = os.path.join(LOCAL_DATA_DIR, safe_name)
        
        # 3. Download from Local API to Container Disk
        await download_file_from_local_api(local_download_url, target_path)
        
        count = len([f for f in Path(LOCAL_DATA_DIR).glob("*") if f.suffix.lower() == ".txt"])
        size_mb = doc.file_size / (1024 * 1024)
        
        await status_msg.edit_text(
            f"✅ Saved `{safe_name}` ({size_mb:.2f} MB).\n"
            f"Files in DB: {count}\n"
            f"Ready to search!", 
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        await status_msg.edit_text(f"⛔ Error downloading file: {str(e)}")
        print(f"Download Error: {e}")

@router.message(CommandObject(command="u"))
async def handle_scan(message: Message, command: CommandObject):
    args = command.args
    if not args:
        await message.reply_text("Usage: /u domain.com")
        return
    
    keyword = " ".join(args)
    status_msg = await message.reply_text(f"🔍 Scanning database for `{keyword}`...", parse_mode=ParseMode.MARKDOWN)
    
    try:
        results = await perform_search(keyword)
        
        if not results:
            await status_msg.edit_text(f"💔 No matches found for `{keyword}`.", parse_mode=ParseMode.MARKDOWN)
            return
            
        count = len(results)
        header = f"✅ Found {count} matches:\n\n"
        
        if count <= THRESHOLD_FOR_TEXT_OUTPUT:
            body = "\n".join(results)
            full_message = header + body
            
            if len(full_message) > 4000:
                 raise ValueError("Too long for text")
                 
            await status_msg.delete()
            await message.reply_text(full_message, disable_web_page_preview=True)
            
        else:
            temp_filename = f"/tmp/results_{int(asyncio.get_event_loop().time())}.txt"
            with open(temp_filename, "w", encoding="utf-8") as f:
                f.write("\n".join(results))
                
            await status_msg.delete()
            
            await message.answer_document(
                document=FSInputFile(temp_filename),
                caption=f"✅ Found {count} matches for `{keyword}`."
            )
            
            os.remove(temp_filename)
            
    except ValueError:
        temp_filename = f"/tmp/results_fallback_{int(asyncio.get_event_loop().time())}.txt"
        with open(temp_filename, "w", encoding="utf-8") as f:
            f.write("\n".join(results))
        await status_msg.delete()
        await message.answer_document(document=FSInputFile(temp_filename), caption=f"✅ Found {len(results)} matches.")
        os.remove(temp_filename)
        
    except Exception as e:
        await status_msg.edit_text(f"⛔ Scan Error: {str(e)}")
        print(f"Scan Error: {e}")

@router.message(CommandObject(command="list"))
async def handle_list(message: Message):
    files = [f.name for f in Path(LOCAL_DATA_DIR).glob("*") if f.suffix.lower() == ".txt"]
    if not files:
        await message.reply_text("📂 Database is empty.")
        return
    
    text = "📄 **Saved Files:**\n" + "\n".join([f"- {n}" for n in files])
    
    if len(text) > 4000:
        text = text[:4000] + "\n..."
        
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

@router.message(CommandObject(command="clear"))
async def handle_clear(message: Message):
    files = list(Path(LOCAL_DATA_DIR).glob("*"))
    if not files:
        await message.reply_text("🗑️ Nothing to clear.")
        return
    
    count = 0
    for f in files:
        try:
            if f.is_file():
                os.remove(f)
                count += 1
        except OSError:
            pass
            
    await message.reply_text(f"🗑️ Cleared {count} files.")

# ================= MAIN ENTRY POINT =================

async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    
    print("🤖 Starting Hybrid Mode (Aiogram for Msgs, Local API for Files)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
