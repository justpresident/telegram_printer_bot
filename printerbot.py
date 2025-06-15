#!/usr/bin/python3

import logging
import os
import pathlib
import re
import telegram
import tempfile
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, ContextTypes, CallbackQueryHandler
from telegram.ext import filters

# This is the Telegram Bot that prints all input documents. It uses libreoffice to convert non-pdf files
# to pdf, which allows printing almost any document formats and images.
# Before sending files for printing user must enter the password by command "/auth <password>".

# Setup instructions: ###############################################################
# 1. Store your bot's token key (from Telegram BotFather) into the file named below #
token_path = "./token"                                                              #
# 2. Store authentication password into the file named below                        #
password_path = "./auth_password"                                                   #
#####################################################################################

token_key = open(token_path, "r").read().strip()
password = open(password_path, "r").read().strip()

# Safety measure. Files larger than this limit are not accepted
file_size_limit = 64*1024*1024  # 64Mb

# Directory where files for printing should be saved
files_dir = "printed_files"
pathlib.Path(files_dir).mkdir(parents=True, exist_ok=True)

# Maximum number of pages allowed for printing
max_pages_limit = 100

# Configuring logging
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger()
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler("printerbot.log")
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# Store authorized users (user_id instead of chat_id for better forwarding support)
authorized_users = set()

def get_user_identifier(update):
    """Get consistent user identifier that works with forwarded messages"""
    if update.message:
        return update.message.from_user.id
    elif update.callback_query:
        return update.callback_query.from_user.id
    return None

def auth_passed(update):
    """Check if user is authorized (works with forwarded messages)"""
    user_id = get_user_identifier(update)
    return user_id in authorized_users

async def request_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please authorize by \"/auth <password>\".")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_identifier(update)
    username = update.message.from_user.username or f"user_{user_id}"
    logger.info(f"User {username} (ID: {user_id}) /start request")

    if not auth_passed(update):
        return await request_auth(update, context)

    try:
        printer_status = os.popen('lpstat -p').read().strip()
        queue_status = os.popen('lpq').read().strip()

        msg = "🖨️ You are authorized to print! Just send a file here.\n\n"
        msg += f"📊 Current printer status:\n```\n{printer_status}\n```\n\n"
        msg += f"📋 Printer queue:\n```\n{queue_status}\n```"

        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error getting printer status: {e}")
        await update.message.reply_text("🖨️ You are authorized to print! Just send a file here.")

async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_identifier(update)
    username = update.message.from_user.username or f"user_{user_id}"
    logger.info(f"User {username} (ID: {user_id}) /pending request")

    if not auth_passed(update):
        return await request_auth(update, context)

    try:
        msg = os.popen('lpstat -W not-completed').read().strip()
        if not msg:
            msg = "✅ No pending jobs found"
        else:
            msg = f"⏳ Pending jobs:\n```\n{msg}\n```"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error getting pending jobs: {e}")
        await update.message.reply_text("❌ Error checking pending jobs")

async def completed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_identifier(update)
    username = update.message.from_user.username or f"user_{user_id}"
    logger.info(f"User {username} (ID: {user_id}) /completed request")

    if not auth_passed(update):
        return await request_auth(update, context)

    try:
        msg = os.popen('lpstat -W completed | head').read().strip()
        if not msg:
            msg = "📋 No completed jobs found"
        else:
            msg = f"✅ Recent completed jobs:\n```\n{msg}\n```"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error getting completed jobs: {e}")
        await update.message.reply_text("❌ Error checking completed jobs")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_identifier(update)
    username = update.message.from_user.username or f"user_{user_id}"
    logger.info(f"User {username} (ID: {user_id}) /cancel request")

    if not auth_passed(update):
        return await request_auth(update, context)

    if not context.args:
        await update.message.reply_text("❌ Please provide a job ID: `/cancel <job_id>`", parse_mode='Markdown')
        return

    job_id = ''.join(context.args).strip()
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        await update.message.reply_text(f"❌ Invalid job_id '{job_id}'")
        return

    logger.info(f"User {username} (ID: {user_id}) cancel request, job '{job_id}'")

    try:
        result = os.system(f"cancel {job_id}")
        if result == 0:
            await update.message.reply_text(f"✅ Job '{job_id}' cancelled successfully")
        else:
            await update.message.reply_text(f"❌ Failed to cancel job '{job_id}'")
    except Exception as e:
        logger.error(f"Error cancelling job: {e}")
        await update.message.reply_text("❌ Error cancelling job")

async def update_message(context: CallbackContext, msg: telegram.Message, text):
    """Update message with error handling"""
    try:
        await context.bot.edit_message_text(text, msg.chat.id, msg.message_id)
    except Exception as e:
        logger.error(f"Error updating message: {e}")

async def authorize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_identifier(update)
    username = update.message.from_user.username or f"user_{user_id}"
    logger.info(f"User {username} (ID: {user_id}) /authorize request")

    if not context.args:
        await update.message.reply_text("❌ Please provide password: `/auth <password>`", parse_mode='Markdown')
        return

    args = ' '.join(context.args)  # Handle passwords with spaces

    if auth_passed(update):
        logger.info(f"User {username} (ID: {user_id}) tried to authorize multiple times.")
        await update.message.reply_text("✅ You are already authorized!")
        return

    if password == args:
        authorized_users.add(user_id)
        logger.info(f"User {username} (ID: {user_id}) authorized.")
        await update.message.reply_text("🎉 Authorization successful! Now you can print files by sending them.")
    else:
        logger.info(f"User {username} (ID: {user_id}) entered wrong password.")
        await update.message.reply_text("❌ Wrong password!")

def cmd_print_file(file_path):
    """Print file with better error handling"""
    try:
        cmd = f'lpr "{file_path}"'
        logger.info(f"Executing {cmd}")
        result = os.system(cmd)
        return result == 0
    except Exception as e:
        logger.error(f"Error printing file: {e}")
        return False

async def maybe_convert(context: CallbackContext, msg: telegram.Message, file_path):
    """Convert file to PDF if needed"""
    fpath, ext = os.path.splitext(file_path)
    if ext.lower() == ".pdf":
        return file_path, True

    await update_message(context, msg, "🔄 Converting to PDF...")

    try:
        abs_file_path = os.path.abspath(file_path)
        abs_files_dir = os.path.abspath(files_dir)

        cmd = f'timeout 60 libreoffice --headless --convert-to pdf "{abs_file_path}" --outdir "{abs_files_dir}"'
        result = os.system(cmd)

        if result == 0:
            new_path = os.path.join(files_dir, os.path.splitext(os.path.basename(file_path))[0] + '.pdf')
            if os.path.exists(new_path):
                return new_path, True

        logger.error(f"Conversion failed with exit code: {result}")
        return file_path, False
    except Exception as e:
        logger.error(f"Error during conversion: {e}")
        return file_path, False

def get_num_pages(file_path):
    """Get number of pages in PDF with error handling"""
    try:
        num_pages_output = os.popen(f'pdfinfo "{file_path}" | grep Pages').read().strip()
        if num_pages_output:
            return int(''.join(filter(str.isdigit, num_pages_output)))
        return 0
    except Exception as e:
        logger.error(f"Error getting page count: {e}")
        return 0

def get_temp_name_for(file_name: str) -> str:
    """Generate temporary filename"""
    temp_name = next(tempfile._get_candidate_names())
    _, file_extension = os.path.splitext(file_name)
    return temp_name + (file_extension if file_extension else "")

async def text_callback(update: Update, context: CallbackContext):
    """Handle text messages"""
    if not auth_passed(update):
        return await request_auth(update, context)

    await update.message.reply_text("📄 Please send a document or image file to print.\n\n"
                                  "Available commands:\n"
                                  "• `/start` - Show printer status\n"
                                  "• `/pending` - Show pending jobs\n"
                                  "• `/completed` - Show completed jobs\n"
                                  "• `/cancel <job_id>` - Cancel a print job")

async def upload_file(update: Update, context: CallbackContext):
    """Handle file uploads"""
    user_id = get_user_identifier(update)
    username = update.message.from_user.username or f"user_{user_id}"
    logger.info(f"User {username} (ID: {user_id}) file upload")

    if not auth_passed(update):
        return await request_auth(update, context)

    # Handle different file types
    if update.message.document is not None:
        file_obj = update.message.document
        file_id = file_obj.file_id
        file_size = file_obj.file_size
        file_name = file_obj.file_name or "document"
        file_type = "document"
    elif update.message.photo is not None:
        file_obj = update.message.photo[-1]  # Get highest resolution
        file_id = file_obj.file_id
        file_size = file_obj.file_size
        file_name = f"photo_{file_obj.file_unique_id}.jpg"
        file_type = "photo"
    else:
        logger.info("Unknown message type")
        await update.message.reply_text("❌ Unsupported file type. Please send a document or photo.")
        return

    # Check file size
    if file_size > file_size_limit:
        await update.message.reply_text(f"❌ File is too large ({file_size:,} bytes > {file_size_limit:,} bytes limit)!")
        return

    reply_msg = await update.message.reply_text("⬇️ Downloading file...")

    try:
        # Generate temporary filename
        temp_name = get_temp_name_for(file_name)
        file_path = os.path.join(files_dir, temp_name)

        logger.info(f"Downloading {file_type} '{file_name}' from {username} (ID: {user_id})...")

        # Download file
        new_file = await file_obj.get_file()
        await new_file.download_to_drive(custom_path=file_path)
        logger.info(f"Downloaded file '{file_name}' as '{temp_name}'")

        # Convert to PDF if needed
        converted_path, success = await maybe_convert(context, reply_msg, file_path)
        if not success:
            await update_message(context, reply_msg, f"❌ Failed to convert file '{file_name}'!")
            return

        logger.info(f"File processed: {converted_path}")

        # Get page count
        num_pages = get_num_pages(converted_path)
        logger.info(f"Number of pages: {num_pages}")

        # Check page limit
        if num_pages > max_pages_limit:
            await update_message(context, reply_msg,
                                f"❌ Too many pages ({num_pages} > {max_pages_limit} limit)!")
            # Clean up file
            try:
                os.unlink(converted_path)
                if converted_path != file_path:
                    os.unlink(file_path)
            except:
                pass
            return

        # Delete status message
        try:
            await context.bot.delete_message(reply_msg.chat.id, reply_msg.message_id)
        except:
            pass

        # Create response with buttons
        keyboard = [
            [telegram.InlineKeyboardButton("🖨️ Print", callback_data=f"print {converted_path}")],
            [telegram.InlineKeyboardButton("🗑️ Delete", callback_data=f"delete {converted_path}")],
        ]
        reply_markup = telegram.InlineKeyboardMarkup(keyboard)

        page_text = f"{num_pages} page{'s' if num_pages != 1 else ''}"
        await update.message.reply_text(f"📄 Ready to print: {page_text}", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error processing file: {e}")
        await update_message(context, reply_msg, f"❌ Error processing file: {str(e)}")

def check_file(file_path):
    """Security check for file path"""
    try:
        # Ensure file is in the correct directory and exists
        abs_file_path = os.path.abspath(file_path)
        abs_files_dir = os.path.abspath(files_dir)

        return (abs_file_path.startswith(abs_files_dir) and
                os.path.exists(abs_file_path) and
                os.path.isfile(abs_file_path))
    except:
        return False

async def button(update: Update, context: CallbackContext):
    """Handle button callbacks"""
    query = update.callback_query
    user_id = get_user_identifier(update)
    username = query.from_user.username or f"user_{user_id}"

    logger.info(f"User {username} (ID: {user_id}) clicked button: {query.data}")

    if not auth_passed(update):
        await query.answer("❌ Not authorized")
        return

    # Answer callback query
    await query.answer()

    try:
        cmd, file_path = query.data.split(' ', 1)
    except ValueError:
        await update_message(context, query.message, "❌ Invalid button data")
        return

    if not check_file(file_path):
        await update_message(context, query.message, "❌ File not found or invalid path")
        return

    if cmd == 'delete':
        try:
            os.unlink(file_path)
            await update_message(context, query.message, "🗑️ File deleted")
        except Exception as e:
            logger.error(f"Error deleting file: {e}")
            await update_message(context, query.message, "❌ Error deleting file")
    elif cmd == 'print':
        await print_file(context, query.message, file_path, username, user_id)
    else:
        await update_message(context, query.message, "❌ Unknown command")

async def print_file(context: CallbackContext, msg: telegram.Message, file_path, username=None, user_id=None):
    """Print file with comprehensive logging"""
    try:
        num_pages = get_num_pages(file_path)
        logger.info(f"Printing file {file_path} for user {username} (ID: {user_id}). Pages: {num_pages}")

        if cmd_print_file(file_path):
            await update_message(context, msg, f"🖨️ File sent to printer! ({num_pages} pages)")
            logger.info(f"Successfully sent {file_path} to printer")
        else:
            await update_message(context, msg, "❌ Failed to send file to printer")
            logger.error(f"Failed to send {file_path} to printer")
    except Exception as e:
        logger.error(f"Error in print_file: {e}")
        await update_message(context, msg, "❌ Error processing print request")

def main() -> None:
    """Main function"""
    try:
        application = Application.builder().token(token_key).build()

        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("auth", authorize))
        application.add_handler(CommandHandler("pending", pending))
        application.add_handler(CommandHandler("completed", completed))
        application.add_handler(CommandHandler("cancel", cancel))

        # File handlers - using the new filters system
        application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, upload_file))
        application.add_handler(CallbackQueryHandler(button))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_callback))

        logger.info("🤖 Telegram Printer Bot starting...")
        logger.info(f"📁 Files directory: {os.path.abspath(files_dir)}")
        logger.info(f"📄 Max file size: {file_size_limit:,} bytes")
        logger.info(f"📋 Max pages: {max_pages_limit}")

        # Run the bot
        application.run_polling(allowed_updates=Update.ALL_TYPES)

    except Exception as e:
        logger.error(f"Fatal error starting bot: {e}")
        raise

if __name__ == '__main__':
    main()

