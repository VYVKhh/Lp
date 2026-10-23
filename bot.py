import os
import re
import mmap
import asyncio
from pathlib import Path
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ================= CONFIGURATION FROM ENVIRONMENT =================
# في Railway، تضع BOT_TOKEN في تبويب Variables
BOT_TOKEN = os.getenv("BOT_TOKEN") 

# مسار التخزين الدائم الذي سنربطه بـ Volume في Railway
# تأكد من أن هذا المسار موجود ومصرح بالكتابة عليه
DATA_FOLDER = "/app/data"  

MAX_FILE_SIZE_MB = 50
THRESHOLD_FOR_TEXT_OUTPUT = 20

SEARCH_PART = "url"
OUTPUT_FORMAT = "combo"
CASE_SENSITIVE = False
USE_REGEX = False

# التحقق من وجود التوكن عند بدء التشغيل
if not BOT_TOKEN:
    raise ValueError("⛔ ERROR: BOT_TOKEN environment variable is missing! Set it in Railway Dashboard.")

# إنشاء مجلد البيانات إذا لم يكن موجوداَ (مهم لأول تشغيل)
os.makedirs(DATA_FOLDER, exist_ok=True)

# ... (نفس دوال parse_ulp_line, search_in_file, perform_search السابقة دون تغيير) ...

def parse_ulp_line(line: str) -> tuple[str, str, str]:
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
    login_s = rest[:sep]
    pass_s = rest[sep+1:]
    return url_s, login_s, pass_s

def search_in_file(filepath: str, keyword: str) -> list[str]:
    results = []
    try:
        size = os.path.getsize(filepath)
        if size == 0: return results
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
                    try: decoded = raw.decode("utf-8", errors="ignore").strip()
                    except UnicodeDecodeError: continue
                    u, l, p = parse_ulp_line(decoded)
                    target = u if SEARCH_PART == "url" else (l if SEARCH_PART == "login" else (p if SEARCH_PART == "password" else decoded))
                    match_found = False
                    if CASE_SENSITIVE:
                        if keyword in target: match_found = True
                    else:
                        if kw_lower in target.lower(): match_found = True
                    if match_found:
                        out_str = f"{l}:{p}" if OUTPUT_FORMAT == "combo" else decoded
                        if out_str: results.append(out_str)
    except Exception as e:
        print(f"Error scanning {filepath}: {e}")
    return results

async def perform_search(keyword: str) -> list[str]:
    all_results = []
    seen_set = set()
    folder_path = Path(DATA_FOLDER)
    if not folder_path.exists(): return []
    files = list(folder_path.glob("*.txt"))
    if not files: return []
    for fp in files:
        res = await asyncio.to_thread(search_in_file, str(fp), keyword)
        for r in res:
            if r not in seen_set:
                seen_set.add(r)
                all_results.append(r)
    return all_results

# ... (نفس Handlers السابقة start, handle_document, handle_scan, etc.) ...

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎨 *ILPicasso Personal Scanner (Railway Edition)*\n\n"
        "**Commands:**\n"
        "`/u <domain>` : Search in saved DB.\n"
        "`/list` : Show saved files.\n"
        "`/clear` : Delete all files.\n"
        "\nSend `.txt` files to add them."
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    if not doc: return
    filename = doc.file_name or "unknown.txt"
    if not filename.lower().endswith(".txt"):
        await msg.reply_text("❌ Only .txt files accepted.")
        return
    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await msg.reply_text(f"❌ File too large! Max {MAX_FILE_SIZE_MB} MB.")
        return

    status_msg = await msg.reply_text("📥 Downloading...")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        safe_filename = "".join(c for c in filename if c.isalnum() or c in ('.', '_', '-')).rstrip()
        if not safe_filename: safe_filename = "uploaded.txt"
        
        # التأكد من أن المسار كامل وصحيح
        local_path = os.path.join(DATA_FOLDER, safe_filename)
        
        await tg_file.download_to_drive(custom_path=local_path)
        
        count = len(list(Path(DATA_FOLDER).glob("*.txt")))
        await status_msg.edit_text(f"✅ Saved `{safe_filename}`.\nTotal files: {count}")
    except Exception as e:
        await status_msg.edit_text(f"⛔ Error: {str(e)}")

async def handle_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    args = context.args
    if not args:
        await msg.reply_text("❌ Usage: `/u domain.com`")
        return
    keyword = " ".join(args)
    status_msg = await msg.reply_text(f"🔍 Scanning for `{keyword}`...")
    try:
        results = await perform_search(keyword)
        if not results:
            await status_msg.edit_text(f"💔 No matches for `{keyword}`.")
            return
        count = len(results)
        header = f"✅ Found {count} matches:\n\n"
        if count <= THRESHOLD_FOR_TEXT_OUTPUT:
            body = "\n".join(results)
            full_message = header + body
            if len(full_message) > 4000: raise ValueError("Too long")
            await status_msg.delete()
            await msg.reply_text(full_message, disable_web_page_preview=True)
        else:
            temp_filename = f"results_{int(asyncio.get_event_loop().time())}.txt"
            with open(temp_filename, "w", encoding="utf-8") as f:
                f.write("\n".join(results))
            await status_msg.delete()
            with open(temp_filename, "rb") as f_out:
                await msg.reply_document(document=f_out, caption=f"✅ Found {count} matches.", filename=temp_filename)
            os.remove(temp_filename)
    except ValueError:
        temp_filename = f"results_{int(asyncio.get_event_loop().time())}.txt"
        with open(temp_filename, "w", encoding="utf-8") as f:
            f.write("\n".join(results))
        await status_msg.delete()
        with open(temp_filename, "rb") as f_out:
            await msg.reply_document(document=f_out, caption=f"✅ Found {len(results)} matches.")
        os.remove(temp_filename)
    except Exception as e:
        await status_msg.edit_text(f"⛔ Scan Error: {str(e)}")

async def handle_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = list(Path(DATA_FOLDER).glob("*.txt"))
    if not files:
        await update.message.reply_text("📂 Database empty.")
        return
    names = [f.name for f in files]
    text = "📄 **Saved Files:**\n" + "\n".join([f"- {n}" for n in names])
    if len(text) > 4000: text = text[:4000] + "\n..."
    await update.message.reply_text(text)

async def handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = list(Path(DATA_FOLDER).glob("*.txt"))
    if not files:
        await update.message.reply_text("🗑️ Nothing to clear.")
        return
    count = 0
    for f in files:
        try:
            os.remove(f)
            count += 1
        except OSError: pass
    await update.message.reply_text(f"🗑️ Cleared {count} files.")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("u", handle_scan))
    app.add_handler(CommandHandler("list", handle_list))
    app.add_handler(CommandHandler("clear", handle_clear))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    print("🤖 Bot started on Railway...")
    app.run_polling()

if __name__ == "__main__":
    main()
