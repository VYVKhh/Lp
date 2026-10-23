import os
import mmap
import asyncio
from pathlib import Path
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, FSInputFile
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATA_FOLDER = "/app/data"  # مسار التخزين الدائم (Volume)
MAX_FILE_SIZE_MB = 50      # حد أقصى للملف
THRESHOLD_FOR_TEXT_OUTPUT = 20 # عدد النتائج التي تُرسل كنص قبل تحويلها لملف

if not BOT_TOKEN:
    raise ValueError("Error: BOT_TOKEN environment variable is missing.")

os.makedirs(DATA_FOLDER, exist_ok=True)

# Initialize Bot and Dispatcher
router = Router()
dp = Dispatcher()
dp.include_router(router)

# ================= CORE LOGIC (Same as before, optimized for sync/async mix) =================

def parse_line(line: str):
    """Extracts url, login, pass from 'url:login:pass' format."""
    line = line.strip()
    if not line: return "", "", ""
    
    c1 = line.find(":")
    if c1 == -1: return line, "", ""
    
    url_end = c1
    # Handle protocols like https://
    if len(line) > c1 + 2 and line[c1:c1+3] == "://":
        c2 = line.find(":", c1 + 3)
        if c2 != -1: url_end = c2
        
    url_s = line[:url_end]
    rest = line[url_end + 1:]
    sep = rest.find(":")
    if sep == -1: return url_s, rest, ""
    
    return url_s, rest[:sep], rest[sep+1:]

def search_file_sync(filepath: str, keyword: str) -> list:
    """Searches a single file using mmap. Runs in thread pool to avoid blocking event loop."""
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
                    
                    # Search in URL only
                    if kw_lower in u.lower():
                        # Output format: combo (login:password)
                        if l and p:
                            results.append(f"{l}:{p}")
    except Exception as e:
        print(f"Scan error in {filepath}: {e}")
    return results

async def perform_search(keyword: str) -> list:
    """Scans all .txt files asynchronously."""
    all_hits = set()
    folder_path = Path(DATA_FOLDER)
    
    if not folder_path.exists():
        return []
        
    files = list(folder_path.glob("*.txt"))
    if not files:
        return []

    # Process each file in a separate thread to keep the bot responsive
    tasks = []
    for fp in files:
        task = asyncio.create_task(
            asyncio.to_thread(search_file_sync, str(fp), keyword)
        )
        tasks.append(task)
    
    # Gather results
    for future in asyncio.as_completed(tasks):
        res = await future
        all_hits.update(res)
        
    return sorted(list(all_hits))

# ================= HANDLERS =================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎨 *ILPicasso Scanner (Aiogram Edition)*\n\n"
        "**Commands:**\n"
        "• Send `.txt` file to add to DB.\n"
        "• `/u <domain>` : Search in saved DB.\n"
        "• `/list` : Show saved files.\n"
        "• `/clear` : Delete all files.",
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(F.document & F.document.file_name.regexp(r".*\.txt$"))
async def handle_document(message: Message):
    doc = message.document
    
    # Check size limit
    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.reply_text(f"❌ File too large! Max {MAX_FILE_SIZE_MB} MB.")
        return

    status_msg = await message.reply_text("📥 Downloading...")
    
    try:
        # Get file path from Telegram
        file_info = await message.bot.get_file(doc.file_id)
        
        # Sanitize filename
        safe_name = "".join(c for c in doc.file_name if c.isalnum() or c in ('.', '_', '-')).rstrip()
        if not safe_name: safe_name = f"uploaded_{doc.file_unique_id}.txt"
        
        local_path = os.path.join(DATA_FOLDER, safe_name)
        
        # Download to drive
        await file_info.download(custom_path=local_path)
        
        count = len(list(Path(DATA_FOLDER).glob("*.txt")))
        await status_msg.edit_text(f"✅ Saved `{safe_name}`.\nTotal files in DB: {count}", parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        await status_msg.edit_text(f"⛔ Error saving file: {str(e)}")
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
            
            # Safety check for message length (Telegram limit ~4096 chars)
            if len(full_message) > 4000:
                 # Switch to file mode if text is unexpectedly long
                 raise ValueError("Too long for text")
                 
            await status_msg.delete()
            await message.reply_text(full_message, disable_web_page_preview=True)
            
        else:
            # Create temporary file for results
            temp_filename = f"/tmp/results_{int(asyncio.get_event_loop().time())}.txt"
            with open(temp_filename, "w", encoding="utf-8") as f:
                f.write("\n".join(results))
                
            await status_msg.delete()
            
            # Send as document
            await message.answer_document(
                document=FSInputFile(temp_filename),
                caption=f"✅ Found {count} matches for `{keyword}`."
            )
            
            # Cleanup temp file
            os.remove(temp_filename)
            
    except ValueError:
        # Fallback logic if text was too long
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
    files = list(Path(DATA_FOLDER).glob("*.txt"))
    if not files:
        await message.reply_text("📂 Database is empty.")
        return
    
    names = [f.name for f in files]
    text = "📄 **Saved Files:**\n" + "\n".join([f"- {n}" for n in names])
    
    # Truncate if too many files
    if len(text) > 4000:
        text = text[:4000] + "\n..."
        
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

@router.message(CommandObject(command="clear"))
async def handle_clear(message: Message):
    files = list(Path(DATA_FOLDER).glob("*.txt"))
    if not files:
        await message.reply_text("🗑️ Nothing to clear.")
        return
    
    count = 0
    for f in files:
        try:
            os.remove(f)
            count += 1
        except OSError:
            pass
            
    await message.reply_text(f"🗑️ Cleared {count} files from database.")

# ================= MAIN ENTRY POINT =================

async def main():
    # Initialize Bot with default properties
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    
    print("🤖 Starting Aiogram Bot on Railway...")
    
    # Start polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
