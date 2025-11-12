import os
import io
import json
import logging
import asyncio # <-- Added import
from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

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

# --- Google Drive Section ---
# (This section is unchanged)

def get_drive_service():
    """
    Creates the Google Drive service using the service account credentials file.
    """
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    service = build("drive", "v3", credentials=creds)
    return service

def upload_to_drive(service, file_stream, file_name):
    """
    Uploads the file (in memory) to the specified Google Drive folder.
    """
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
        return file.get('webViewLink') # Returns the file's view link
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return None

# --- Telegram Bot Section ---
# (This section is unchanged)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Response to the /start command"""
    await update.message.reply_text("سلام! 👋\nهر فایل، عکس یا فیلمی بفرستی، من آن را در گوگل درایو ذخیره می‌کنم.")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    This function is executed when any file, photo, or video is sent.
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
            
    except Exception as e:
        logger.error(f"General error processing file: {e}")
        await status_message.edit_text(f"خطا: {e}")

# --- Bot and Flask Initialization ---

# Initialize the Flask application
app = Flask(__name__)

# Initialize the Telegram bot application
telegram_app = (
    Application.builder().token(TOKEN).build()
)

# Add commands to the bot
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(
    MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO, 
        handle_file
    )
)

# --- Webhook Setup Function (Modified) ---
async def setup_bot_and_webhook():
    """
    Initializes the bot application AND sets the webhook.
    """
    logger.info("Initializing application...")
    await telegram_app.initialize() # <-- THIS IS THE FIX
    
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL is not set in environment variables.")
        return
        
    full_webhook_url = f"{WEBHOOK_URL}/webhook"
    
    logger.info(f"Setting webhook to {full_webhook_url}...")
    await telegram_app.bot.set_webhook(full_webhook_url)
    logger.info("Webhook set successfully.")


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
    if request.method == "POST":
        try:
            update = Update.de_json(request.get_json(force=True), telegram_app.bot)
            await telegram_app.process_update(update)
            return jsonify({"status": "ok"})
        except Exception as e:
            # Log the *actual* error
            logger.error(f"Error processing update: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "invalid method"}), 405

# --- NEW: Run setup when Gunicorn starts ---
# This code runs when Gunicorn imports the file (i.e., when the worker starts)
if __name__ != "__main__":
    try:
        logger.info("Running initial setup...")
        asyncio.run(setup_bot_and_webhook())
        logger.info("Initial setup complete.")
    except Exception as e:
        logger.error(f"Failed to run initial setup: {e}")

# --- OLD: /setup route (REMOVED) ---
# We don't need this route anymore, setup runs automatically
# @app.route("/setup")
# def setup():
#     ...

# This part is only for local testing
if __name__ == "__main__":
    # Note: Local testing might behave differently now
    # It's best to rely on the Render deployment
    logger.info("Running in local debug mode...")
    app.run(debug=True, port=5001)
