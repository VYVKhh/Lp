import os
import mmap
import asyncio
import shutil
from pathlib import Path
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Error: BOT_TOKEN environment variable is missing.")

LOCAL_API_URL = "http://127.0.0.1:8081"
LOCAL_DATA_DIR = "/app/data"
os.makedirs(LOCAL_DATA_DIR, exist_ok=True)

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

def count_lines_sync(filepath: str) -> int:
    count = 0
    try:
        if os.path.getsize(filepath) == 0: return 0
        with open(filepath, "rb") as fh:
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                sz = mm.size()
                pos = 0
                while pos < sz:
                    end = mm.find(b"\n", pos)
                    if end == -1:
                        if pos < sz:
                            count += 1
                        break
                    raw = mm[pos:end]
                    pos = end + 1
                    if raw.strip(b"\r"):
                        count += 1
                    elif raw:
                        count += 1
    except Exception as e:
        print(f"Count error in {filepath}: {e}")
    return count

async def perform_search(keyword: str) -> list:
    all_hits = set()
    folder_path = Path(LOCAL_DATA_DIR)
    if not folder_path.exists():
        return []
    files = list(folder_path.rglob("*.txt"))
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

async def compute_stats():
    folder_path = Path(LOCAL_DATA_DIR)
    if not folder_path.exists():
        return 0, 0
    files = list(folder_path.rglob("*.txt"))
    if not files:
        return 0, 0
    tasks = [asyncio.to_thread(count_lines_sync, str(fp)) for fp in files]
    results = await asyncio.gather(*tasks)
    return len(files), sum(results)

# ================= HANDLERS =================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎨 *ILPicasso Scanner (Local API Edition)*\n\n"
        "**Limits Raised:** Supports files up to 2GB.\n\n"
        "**Workflow:**\n"
        "1. Send `.txt` file.\n"
        "2. Wait for download confirmation.\n"
        "3. Use `/u <domain>` to search.\n"
        "4. Use `/s` to see stats.",
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(F.document & F.document.file_name.regexp(r".*\.txt$"))
async def handle_document(message: Message):
    doc = message.document

    if doc.file_size and doc.file_size > MAX_FILE_SIZE_BYTES:
        await message.answer(f"❌ File too large! Max allowed is 2GB.\nYour file: {doc.file_size / (1024*1024*1024):.2f} GB")
        return

    status_msg = await message.answer("📥 Processing via Local API...")

    try:
        file = await message.bot.get_file(doc.file_id)
        source_path = file.file_path

        print(f"DEBUG: Local file path returned by API: {source_path}")

        if not source_path or not os.path.exists(source_path):
            raise FileNotFoundError(f"File not found locally at {source_path}. Check API logs.")

        print(f"DEBUG: Found local source file at: {source_path}")

        safe_name = "".join(c for c in doc.file_name if c.isalnum() or c in ('.', '_', '-')).rstrip()
        if not safe_name:
            safe_name = f"uploaded_{doc.file_unique_id}.txt"

        target_path = os.path.join(LOCAL_DATA_DIR, safe_name)

        shutil.copy2(source_path, target_path)

        count = len(list(Path(LOCAL_DATA_DIR).rglob("*.txt")))
        size_mb = (doc.file_size or 0) / (1024 * 1024)

        await status_msg.edit_text(
            f"✅ Saved `{safe_name}` ({size_mb:.2f} MB).\n"
            f"Files in DB: {count}\n"
            f"Ready to search!",
            parse_mode=ParseMode.MARKDOWN
        )

    except asyncio.TimeoutError:
        await status_msg.edit_text("⏳ Timeout while downloading the file from Telegram, please try again.")
        print("Download Timeout Error")

    except Exception as e:
        await status_msg.edit_text(f"⛔ Error processing file: {str(e)}")
        print(f"Download/Copy Error: {e}")
        import traceback
        traceback.print_exc()

@router.message(Command("u"))
async def handle_scan(message: Message):
    text = message.text or ""
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer("Usage: /u domain.com")
        return

    keyword = parts[1].strip()

    status_msg = await message.answer(f"🔍 Scanning database for `{keyword}`...", parse_mode=ParseMode.MARKDOWN)

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
            await message.answer(full_message, disable_web_page_preview=True)

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

@router.message(Command("s"))
async def handle_stats(message: Message):
    status_msg = await message.answer("📊 Calculating stats...")

    try:
        file_count, total_lines = await compute_stats()

        await status_msg.edit_text(
            f"📊 *Database Stats*\n\n"
            f"Files: `{file_count}`\n"
            f"Total lines: `{total_lines}`",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        await status_msg.edit_text(f"⛔ Stats Error: {str(e)}")
        print(f"Stats Error: {e}")

@router.message(Command("list"))
async def handle_list(message: Message):
    files = [f.name for f in Path(LOCAL_DATA_DIR).rglob("*.txt")]
    if not files:
        await message.answer("📂 Database is empty.")
        return

    text = "📄 **Saved Files:**\n" + "\n".join([f"- {n}" for n in files])

    if len(text) > 4000:
        text = text[:4000] + "\n..."

    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("clear"))
async def handle_clear(message: Message):
    files = list(Path(LOCAL_DATA_DIR).rglob("*"))
    if not files:
        await message.answer("🗑️ Nothing to clear.")
        return

    count = 0
    for f in files:
        try:
            if f.is_file():
                os.remove(f)
                count += 1
        except OSError:
            pass

    await message.answer(f"🗑️ Cleared {count} files.")

# ================= MAIN ENTRY POINT =================

async def main():
    session = AiohttpSession(
        api=TelegramAPIServer.from_base(LOCAL_API_URL, is_local=True),
        timeout=300,
    )
    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    print("🤖 Starting Local API Mode (Bot fully routed through local server)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
