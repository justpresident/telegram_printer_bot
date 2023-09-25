#!/usr/bin/python3

import logging
import os
import pathlib
import re
import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, ContextTypes, CallbackQueryHandler

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


async def request_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please authorize by \"/auth <password>\".")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("User {} /start request".format(update.message.from_user.username))
    if not auth_passed(update):
        return await request_auth(update, context)

    msg = "You are authorized to print, just send a file here.\n"
    msg += "Current state:\n" + os.popen('lpstat -p').read() + "\n"
    msg += "Printer queue:\n" + os.popen('lpq').read()
    await update.message.reply_text(msg)

async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("User {} /pending request".format(update.message.from_user.username))
    if not auth_passed(update):
        return request_auth(update, context)

    msg = os.popen('lpstat -W not-completed').read()
    if msg == '':
        msg = "No jobs found"
    await update.message.reply_text(msg)

async def completed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("User {} /completed request".format(update.message.from_user.username))
    if not auth_passed(update):
        return await request_auth(update, context)

    msg = os.popen('lpstat -W completed | head').read()
    if msg == '':
        msg = "No jobs found"
    await update.message.reply_text(msg)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("User {} /cancel request".format(update.message.from_user.username))
    if not auth_passed(update):
        return await request_auth(update, context)

    job_id = ''.join(context.args).strip()
    if not re.match('^[a-zA-Z0-9_\-]+$', job_id):
        await update.message.reply_text("Invalid job_id '{}'".format(job_id))
        return
    logger.info("User {} cancel request, job '{}'".format(update.message.from_user.username, job_id))
    os.system("cancel {}".format(job_id))
    await update.message.reply_text("Cancel command complete")


authorized_chats = set()
def auth_passed(update):
    return update.message.chat_id in authorized_chats


async def update_message(context: CallbackContext, msg: telegram.Message, text):
    await context.bot.edit_message_text(text, msg.chat.id, msg.message_id)

async def authorize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("User {} /authorize request".format(update.message.from_user.username))
    args = ''.join(context.args)

    if auth_passed(update):
        logger.info("User {} tried to authorize multiple times.".format(update.message.from_user.username))
        await update.message.reply_text("You already authorized!")
        return

    if password == args:
        authorized_chats.add(update.effective_chat.id)
        logger.info("User {} authorized.".format(update.message.from_user.username))
        await update.message.reply_text("Now you can print files via sending.")
    else:
        logger.info("User {} entered wrong password: {}.".format(update.message.from_user.username, args))
        await update.message.reply_text("Wrong password!")


def cmd_print_file(file_path):
    cmd = 'lpr "{}"'.format(file_path)
    logger.info("Executing {}".format(cmd))
    os.system(cmd)

async def maybe_convert(context: CallbackContext, msg: telegram.Message, file_path):
    (fpath, ext) = os.path.splitext(file_path);
    if ext.lower() == ".pdf":
        return (file_path, True)

    await update_message(context, msg, "Converting to pdf...")

    if 0 == os.system('timeout 30 libreoffice --convert-to "pdf" "{}" --outdir {}'.format(file_path, files_dir)):
        new_path = files_dir + '/' + os.path.splitext(os.path.basename(file_path))[0] + '.pdf'
        return (new_path, True )
    else:
        return (file_path, False)

def get_num_pages(file_path):
    num_pages = os.popen('pdfinfo "{}" | grep Pages'.format(file_path)).read().strip()
    return int(''.join(filter(str.isdigit, num_pages)))


async def text_callback(update: Update, context: CallbackContext):
    await update.message.reply_text("Use commands")

async def upload_file(update: Update, context: CallbackContext):
    logger.info("User {} file upload".format(update.message.from_user.username))
    if not auth_passed(update):
        return await request_auth(update, context)
    if update.message.document is not None:
        file_id = update.message.document.file_id
        file_size = update.message.document.file_size
        file_name = update.message.document.file_name
        new_file = await update.message.document.get_file()
    elif update.message.photo is not None:
        file_id = update.message.photo[-1].file_id
        file_size = update.message.photo[-1].file_size
        file_name = update.message.photo[-1].file_unique_id
        new_file = await update.message.photo[-1].get_file()
    else:
        logger.info("Unknown message type")
        return

    if file_size > file_size_limit:
        await update.message.reply_text("File is too big ({} > {})!".format(file_size, file_size_limit))
        return

    reply_msg = await update.message.reply_text("Downloading file...")

    file_path = files_dir + '/' + file_name

    logger.info("Downloading file {} from {}...".format(file_name, update.message.from_user.username))
    await new_file.download_to_drive(custom_path=file_path)
    logger.info("Downloaded  file {} from {}!".format(file_name, update.message.from_user.username))

    (file_path, success) = await maybe_convert(context, reply_msg, file_path)
    if not success:
        update_message(context, reply_msg, "Failed to convert file {}, size {}!".format(file_name, file_size))
        return

    # Delete upload status message after successful upload and convert
    context.bot.delete_message(reply_msg.chat.id, reply_msg.message_id)

    num_pages = get_num_pages(file_path)

    keyboard = [
        [telegram.InlineKeyboardButton("Print", callback_data="print {}".format(file_path))],
        [telegram.InlineKeyboardButton("Delete file", callback_data="delete {}".format(file_path))],
    ]
    reply_markup = telegram.InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Num pages: {}".format(num_pages), reply_markup=reply_markup)


def check_file(file_path):
    return file_path == files_dir + '/' + os.path.basename(file_path)

async def button(update: Update, context: CallbackContext):
    query = update.callback_query
    logger.info("User {} clicked button: ".format(query.data))
    if not auth_passed(query):
        return await request_auth(update, context)

    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()

    (cmd, file_path) = query.data.split(' ', 1)
    if not check_file(file_path):
        return

    if cmd == 'delete':
        os.unlink(file_path)
        await update_message(context, query.message, "Deleted")
    elif cmd == 'print':
        await print_file(context, query.message, file_path)
    else:
        await update_message(context, query.message, f"WAT?")


async def print_file(context: CallbackContext, msg: telegram.Message, file_path):
    num_pages = get_num_pages(file_path)
    logger.info("Printing file {}. Number of pages: {}".format(file_path, num_pages))
    cmd_print_file(file_path)
    await update_message(context, msg, "File was sent for printing!")


def main() -> None:
    application = Application.builder().token(token_key).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("auth", authorize))
    application.add_handler(CommandHandler("pending", pending))
    application.add_handler(CommandHandler("completed", completed))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters=telegram.ext.filters.Document.ALL, callback=upload_file))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters=telegram.ext.filters.TEXT, callback=text_callback))


    # Run the bot until the user presses Ctrl-C
    logger.info("Listening...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

