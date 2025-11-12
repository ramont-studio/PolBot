import os
import io
import json
import logging
from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- Configuration Section ---
# These values are read from Environment Variables in Render
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
# This is the Google credentials JSON file, added as a "Secret File" in Render
# We set its path in this environment variable
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "google_creds.json")

# Your public URL on Render (for setting the Webhook)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") 

# Logging setup (for debugging in Render)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Google Drive Section ---

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Response to the /start command"""
    # User-facing language remains Persian
    await update.message.reply_text("سلام! 👋\nهر فایل، عکس یا فیلمی بفرستی، من آن را در گوگل درایو ذخیره می‌کنم.")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    This function is executed when any file, photo, or video is sent.
    """
    message = update.message
    file_name = ""
    file_to_process = None

    # Detect file type
    if message.document:
        file_to_process = message.document
        file_name = message.document.file_name
    elif message.video:
        file_to_process = message.video
        file_name = message.video.file_name or f"video_{message.video.file_unique_id}.mp4"
    elif message.photo:
        # Photos in Telegram have multiple sizes, we get the largest (last) one
        file_to_process = message.photo[-1]
        file_name = f"photo_{file_to_process.file_unique_id}.jpg"
    elif message.audio:
        file_to_process = message.audio
        file_name = message.audio.file_name or f"audio_{message.audio.file_unique_id}.mp3"
    
    if not file_to_process:
        # User-facing language remains Persian
        await message.reply_text("فرمت فایل پشتیبانی نمی‌شود.")
        return

    # Send a "Processing..." message
    # User-facing language remains Persian
    status_message = await message.reply_text("در حال دریافت فایل از تلگرام...")
    
    try:
        # 1. Download the file from Telegram into memory
        file_id = file_to_process.file_id
        bot_file = await context.bot.get_file(file_id)
        
        # Download the file into an in-memory byte stream
        file_stream = io.BytesIO()
        await bot_file.download_to_memory(file_stream)
        file_stream.seek(0) # Rewind the stream to the beginning
        
        # User-facing language remains Persian
        await status_message.edit_text("در حال آپلود در گوگل درایو... ☁️")
        
        # 2. Upload the file to Google Drive
        # Since Google API functions are synchronous,
        # we run them in a separate thread to avoid blocking the bot
        service = await context.application.loop.run_in_executor(None, get_drive_service)
        
        file_link = await context.application.loop.run_in_executor(
            None, upload_to_drive, service, file_stream, file_name
        )

        # 3. Send a success message
        if file_link:
            # User-facing language remains Persian
            await status_message.edit_text(
                f"✅ فایل با موفقیت آپلود شد!\n\n<a href='{file_link}'>{file_name}</a>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        else:
            # User-facing language remains Persian
            await status_message.edit_text("❌ مشکلی در آپلود فایل پیش آمد.")
            
    except Exception as e:
        logger.error(f"General error processing file: {e}")
        # User-facing language remains Persian
        await status_message.edit_text(f"خطا: {e}")

# --- Flask Web Server Section ---

# Initialize the Flask application
app = Flask(__name__)

# Initialize the Telegram bot application
telegram_app = (
    Application.builder().token(TOKEN).build()
)

# Add commands to the bot
telegram_app.add_handler(CommandHandler("start", start))
# This MessageHandler handles all files
telegram_app.add_handler(
    MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO, 
        handle_file
    )
)

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
            logger.warning(f"Error processing update: {e}")
            return jsonify({"status": "error"}), 500
    return jsonify({"status": "invalid method"}), 405

# --- These sections are for initial setup ---
async def setup_webhook():
    """
    Runs once to tell Telegram where to send updates.
    """
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL is not set in environment variables.")
        return
        
    # Build the full webhook URL
    full_webhook_url = f"{WEBHOOK_URL}/webhook"
    
    # Set the webhook
    await telegram_app.bot.set_webhook(full_webhook_url)
    logger.info(f"Webhook set to {full_webhook_url}")

# Special command for setup on Render
# Render can run this after the build
@app.route("/setup")
def setup():
    """
    A route that Render can call to set the webhook.
    This can be called from Render's "Build Command".
    """
    import asyncio
    asyncio.run(setup_webhook())
    return "Webhook setup finished!"

# This part is only for local testing
# gunicorn directly uses the 'app' variable
if __name__ == "__main__":
    app.run(debug=True, port=5001)