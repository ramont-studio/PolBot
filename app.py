import os
import io
import json
import logging
import asyncio
import threading  # <-- 1. Import threading
from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest  # <-- 2. Import specific error

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- Configuration Section ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "google_creds.json")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") 

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Google Drive Section (Unchanged) ---

def get_drive_service():
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    service = build("drive", "v3", credentials=creds)
    return service

def upload_to_drive(service, file_stream, file_name):
    try:
        file_metadata = {
            "name": file_name,
            "parents": [DRIVE_FOLDER_ID]
        }
        media = MediaIoBaseUpload(file_stream, mimetype='application/octet-stream', resumable=True)
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute()
        
        logger.info(f"File uploaded successfully. ID: {file.get('id')}")
        return file.get('webViewLink')
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return None

# --- Telegram Bot Section ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Response to the /start command"""
    await update.message.reply_text("سلام! 👋\nهر فایل، عکس یا فیلمی بفرستی، من آن را در گوگل درایو ذخیره می‌کنم.")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles files, photos, videos, or audio.
    """
    message = update.message
    file_name = ""
    file_to_process = None

    if message.document:
        file_to_process = message.document
        file_name = message.document.file_name
    elif message.video:
        file_to_process = message.video
        file_name = message.video.file_name or f"video_{message.video.file_unique_id}.mp4"
    elif message.photo:
        file_to_process = message.photo[-1]
        file_name = f"photo_{file_to_process.file_unique_id}.jpg"
    elif message.audio:
        file_to_process = message.audio
        file_name = message.audio.file_name or f"audio_{message.audio.file_unique_id}.mp3"
    
    if not file_to_process:
        await message.reply_text("فرمت فایل پشتیبانی نمی‌شود.")
        return

    status_message = await message.reply_text("در حال دریافت فایل از تلگرام...")
    
    try:
        file_id = file_to_process.file_id
        bot_file = await context.bot.get_file(file_id)
        
        file_stream = io.BytesIO()
        await bot_file.download_to_memory(file_stream)
        file_stream.seek(0)
        
        await status_message.edit_text("در حال آپلود در گوگل درایو... ☁️")
        
        service = await context.application.loop.run_in_executor(None, get_drive_service)
        
        file_link = await context.application.loop.run_in_executor(
            None, upload_to_drive, service, file_stream, file_name
        )

        if file_link:
            await status_message.edit_text(
                f"✅ فایل با موفقیت آپلود شد!\n\n<a href='{file_link}'>{file_name}</a>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        else:
            await status_message.edit_text("❌ مشکلی در آپلود فایل پیش آمد.")
            
    except BadRequest as e:
        # <-- 3. Catch the specific "File is too big" error
        if "File is too big" in e.message:
            logger.warning(f"File too big: {file_name}")
            await status_message.edit_text("❌ خطا: فایل خیلی بزرگ است.\nمن فقط می‌توانم فایل‌های تا ۲۰ مگابایت را پردازش کنم.")
        else:
            logger.error(f"BadRequest error processing file: {e}")
            await status_message.edit_text(f"خطای تلگرام: {e.message}")
            
    except Exception as e:
        logger.error(f"General error processing file: {e}")
        await status_message.edit_text(f"خطای ناشناخته: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs the error."""
    # <-- 4. Add a proper error handler
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)


# --- Bot and Flask Initialization ---

app = Flask(__name__)

telegram_app = (
    Application.builder().token(TOKEN).build()
)

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(
    MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO, 
        handle_file
    )
)
telegram_app.add_error_handler(error_handler)  # <-- 5. Register the error handler

# --- Thread-safe setup logic ---
# <-- 6. This is the new, thread-safe setup logic
setup_lock = threading.Lock()
setup_done = False

async def setup_bot_and_webhook():
    """
    Initializes the bot application AND sets the webhook.
    Runs only ONCE.
    """
    logger.info("Initializing application...")
    await telegram_app.initialize()
    
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL is not set.")
        return
        
    full_webhook_url = f"{WEBHOOK_URL}/webhook"
    
    logger.info(f"Setting webhook to {full_webhook_url}...")
    await telegram_app.bot.set_webhook(full_webhook_url)
    logger.info("Webhook set successfully.")
    logger.info("Initial setup complete.")

@app.before_request
def ensure_setup_is_done():
    """
    This function runs before *every* request.
    It uses a lock to ensure the async setup runs exactly once.
    """
    global setup_done
    with setup_lock:
        if not setup_done:
            logger.info("First request received. Running initial setup...")
            try:
                # We need to run the async setup function
                asyncio.run(setup_bot_and_webhook())
                setup_done = True
            except Exception as e:
                logger.error(f"Failed to run initial setup: {e}")

# --- Flask Web Server Routes ---

@app.route("/")
def index():
    """A simple page to make sure the server is alive"""
    return "Hello, I am the DriveBot!"

@app.route("/webhook", methods=["POST"])
async def webhook():
    """
    This is the address where Telegram sends messages (Webhook)
    """
    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        await telegram_app.process_update(update)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Error processing update in webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# We no longer need the 'if __name__ == "__main__":' block
# Gunicorn will handle running the app.
