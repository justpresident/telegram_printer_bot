#!/usr/bin/python3

import logging
import os
import pathlib
import re
import telegram
from telegram import Update
from telegram.ext import CallbackContext, ContextTypes

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


def request_auth(update: Update, context: CallbackContext):
    context.bot.send_message(chat_id=update.effective_chat.id, text="Please authorize by \"/auth <password>\".")


def start(update: Update, context: CallbackContext):
    logger.info("User {} /start request".format(update.message.from_user.username))
    if not auth_passed(update):
        return request_auth(update, context)

    msg = "You are authorized to print, just send a file here.\n"
    msg += "Current state:\n" + os.popen('lpstat -p').read() + "\n"
    msg += "Printer queue:\n" + os.popen('lpq').read()
    context.bot.send_message(chat_id=update.effective_chat.id, text=msg)

def pending(update: Update, context: CallbackContext):
    logger.info("User {} /pending request".format(update.message.from_user.username))
    if not auth_passed(update):
        return request_auth(update, context)

    msg = os.popen('lpstat -W not-completed').read()
    if msg == '':
        msg = "No jobs found"
    context.bot.send_message(chat_id=update.effective_chat.id, text=msg)

def completed(update: Update, context: CallbackContext):
    logger.info("User {} /completed request".format(update.message.from_user.username))
    if not auth_passed(update):
        return request_auth(update, context)

    msg = os.popen('lpstat -W completed | head').read()
    if msg == '':
        msg = "No jobs found"
    context.bot.send_message(chat_id=update.effective_chat.id, text=msg)

def cancel(update: Update, context: CallbackContext):
    logger.info("User {} /cancel request".format(update.message.from_user.username))
    if not auth_passed(update):
        return request_auth(update, context)

    job_id = ''.join(context.args).strip()
    if not re.match('^[a-zA-Z0-9_\-]+$', job_id):
        context.bot.send_message(chat_id=update.effective_chat.id, text="Invalid job_id '{}'".format(job_id))
        return
    logger.info("User {} cancel request, job '{}'".format(update.message.from_user.username, job_id))
    os.system("cancel {}".format(job_id))
    context.bot.send_message(chat_id=update.effective_chat.id, text="Cancel command complete")


authorized_chats = set()
def auth_passed(update):
    return update.message.chat_id in authorized_chats


def update_message(context: CallbackContext, msg: telegram.Message, text):
    context.bot.edit_message_text(text, msg.chat.id, msg.message_id)

def authorize(update: Update, context: CallbackContext):
    logger.info("User {} /authorize request".format(update.message.from_user.username))
    args = ''.join(context.args)

    if auth_passed(update):
        logger.info("User {} tried to authorize multiple times.".format(update.message.from_user.username))
        context.bot.send_message(chat_id=update.effective_chat.id, text="You already authorized!")
        return

    if password == args:
        authorized_chats.add(update.effective_chat.id)
        logger.info("User {} authorized.".format(update.message.from_user.username))
        context.bot.send_message(chat_id=update.effective_chat.id, text="Now you can print files via sending.")
    else:
        logger.info("User {} entered wrong password: {}.".format(update.message.from_user.username, args))
        context.bot.send_message(chat_id=update.effective_chat.id, text="Wrong password!")


def cmd_print_file(file_path):
    cmd = 'lpr "{}"'.format(file_path)
    logger.info("Executing {}".format(cmd))
    os.system(cmd)

def maybe_convert(context: CallbackContext, msg: telegram.Message, file_path):
    (fpath, ext) = os.path.splitext(file_path);
    if ext.lower() == ".pdf":
        return (file_path, True)

    update_message(context, msg, "Converting to pdf...")

    if 0 == os.system('timeout 30 libreoffice --convert-to "pdf" "{}" --outdir {}'.format(file_path, files_dir)):
        new_path = files_dir + '/' + os.path.splitext(os.path.basename(file_path))[0] + '.pdf'
        return (new_path, True )
    else:
        return (file_path, False)

def get_num_pages(file_path):
    num_pages = os.popen('pdfinfo "{}" | grep Pages'.format(file_path)).read().strip()
    return int(''.join(filter(str.isdigit, num_pages)))


def upload_file(update: Update, context: CallbackContext):
    logger.info("User {} file upload".format(update.message.from_user.username))
    if not auth_passed(update):
        return request_auth(update, context)
    if update.message.document is not None:
        file_id = update.message.document.file_id
        file_size = update.message.document.file_size
        file_name = update.message.document.file_name
    elif update.message.photo is not None:
        file_id = update.message.photo[-1].file_id
        file_size = update.message.photo[-1].file_size
        file_name = update.message.photo[-1].file_unique_id
    else:
        logger.info("Unknown message type")
        return

    if file_size > file_size_limit:
        context.bot.send_message(chat_id=update.effective_chat.id, text="File is too big ({} > {})!".format(file_size, file_size_limit))
        return

    reply_msg = context.bot.send_message(chat_id=update.effective_chat.id, text="Downloading file...")

    file_path = files_dir + '/' + file_name

    logger.info("Downloading file {} from {}...".format(file_name, update.message.from_user.username))
    new_file = context.bot.get_file(file_id)
    new_file.download(file_path)
    logger.info("Downloaded  file {} from {}!".format(file_name, update.message.from_user.username))

    (file_path, success) = maybe_convert(context, reply_msg, file_path)
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

    update.message.reply_text("Num pages: {}".format(num_pages), reply_markup=reply_markup)


def check_file(file_path):
    return file_path == files_dir + '/' + os.path.basename(file_path)

def button(update: Update, context: CallbackContext):
    query = update.callback_query
    logger.info("User {} clicked button: ".format(query.data))
    if not auth_passed(query):
        return request_auth(update, context)

    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    query.answer()

    (cmd, file_path) = query.data.split(' ', 1)
    if not check_file(file_path):
        return

    if cmd == 'delete':
        os.unlink(file_path)
        update_message(context, query.message, "Deleted")
    elif cmd == 'print':
        print_file(context, query.message, file_path)
    else:
        update_message(context, query.message, f"WAT?")


def print_file(context: CallbackContext, msg: telegram.Message, file_path):
    num_pages = get_num_pages(file_path)
    logger.info("Printing file {}. Number of pages: {}".format(file_path, num_pages))
    cmd_print_file(file_path)
    update_message(context, msg, "File was sent for printing!")


def main():
    updater = telegram.ext.Updater(token=token_key)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(telegram.ext.CommandHandler('start', start))
    dispatcher.add_handler(telegram.ext.CommandHandler('auth', authorize))
    dispatcher.add_handler(telegram.ext.CommandHandler('pending', pending))
    dispatcher.add_handler(telegram.ext.CommandHandler('completed', completed))
    dispatcher.add_handler(telegram.ext.CommandHandler('cancel', cancel))
    dispatcher.add_handler(telegram.ext.MessageHandler(filters=telegram.ext.Filters.document, callback=upload_file))
    dispatcher.add_handler(telegram.ext.MessageHandler(filters=telegram.ext.Filters.photo, callback=upload_file))
    dispatcher.add_handler(telegram.ext.CallbackQueryHandler(button))

    logger.info("Listening...")
    updater.start_polling()

if __name__ == '__main__':
    main()

