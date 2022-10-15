#!/usr/bin/python3

import os
import pathlib
import logging
from telegram import Update
from telegram.ext import Updater
from telegram.ext import ContextTypes
from telegram.ext import CallbackContext
from telegram.ext import Filters
from telegram.ext import CommandHandler
from telegram.ext import MessageHandler

# This is the Telegram Bot that prints all input documents. It can print pdf and txt files.
# Before sending files for printing user must enter the password by command "/auth <password>".

# TODO: Store your bot's token key (from Telegram BotFather)
tokenpath = "./token"
token_key = open("./token", "r").read().strip()
password = open("./auth_password", "r").read().strip()

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
    logger.info("User {} started...".format(update.message.from_user.username))
    if not auth_passed(update):
        return request_auth(update, context)

    msg = "You are authorized to print, just send a file here.\nSupported formats: PDF, PS, TXT, JPG.\n"
    msg += "Current state:\n" + os.popen('lpstat -p').read() + "\n"
    msg += "Printer queue:\n" + os.popen('lpq').read()
    context.bot.send_message(chat_id=update.effective_chat.id, text=msg)

def pending(update: Update, context: CallbackContext):
    if not auth_passed(update):
        return request_auth(update, context)

    logger.info("User {} pending request".format(update.message.from_user.username))
    msg = os.popen('lpstat -W not-completed').read()
    if msg == '':
        msg = "No jobs found"
    context.bot.send_message(chat_id=update.effective_chat.id, text=msg)

def completed(update: Update, context: CallbackContext):
    if not auth_passed(update):
        return request_auth(update, context)

    logger.info("User {} completed request".format(update.message.from_user.username))
    msg = os.popen('lpstat -W completed | head').read()
    if msg == '':
        msg = "No jobs found"
    context.bot.send_message(chat_id=update.effective_chat.id, text=msg)

def cancel(update: Update, context: CallbackContext):
    if not auth_passed(update):
        return request_auth(update, context)

    job_id = ''.join(context.args).strip()
    if job_id.find(' ') != -1:
        context.bot.send_message(chat_id=update.effective_chat.id, text="Invalid job_id '{}'".format(job_id))
    logger.info("User {} cancel request, job '{}'".format(update.message.from_user.username, job_id))
    os.system("cancel {}".format(job_id))
    context.bot.send_message(chat_id=update.effective_chat.id, text="Cancel command complete")


authorized_chats = set()
def auth_passed(update):
    return update.message.chat_id in authorized_chats


def authorize(update: Update, context: CallbackContext):
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
    os.system('lpr "{}"'.format(file_path))

def maybe_convert(update: Update, context: CallbackContext, file_path):
    (fpath, ext) = os.path.splitext(file_path);
    if ext.lower() == ".pdf":
        return (file_path, True)

    context.bot.send_message(chat_id=update.effective_chat.id, text="Converting to pdf...")

    if 0 == os.system('timeout 5 libreoffice --convert-to "pdf" "{}" --outdir {}'.format(file_path, files_dir)):
        new_path = files_dir + '/' + os.path.splitext(os.path.basename(file_path))[0] + '.pdf'
        return (new_path, True )
    else:
        return (file_path, False)

def print_file(update: Update, context: CallbackContext):
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

    context.bot.send_message(chat_id=update.effective_chat.id, text="Downloading file...")

    file_path = files_dir + '/' + file_name

    logger.info("Downloading file {} from {}...".format(file_name, update.message.from_user.username))
    new_file = context.bot.get_file(file_id)
    new_file.download(file_path)
    logger.info("Downloaded  file {} from {}!".format(file_name, update.message.from_user.username))

    (file_path, success) = maybe_convert(update, context, file_path)
    if not success:
        context.bot.send_message(chat_id=update.effective_chat.id, text="Failed to convert file {}, size {}!".format(file_name, file_size))
        return

    num_pages = os.popen('pdfinfo "{}" | grep Pages'.format(file_path)).read().strip()
    num_pages = int(''.join(filter(str.isdigit, num_pages)))

    logger.info("Printing file {}. Number of pages: {}...".format(file_path, num_pages))
    cmd_print_file(file_path)

    logger.info("File {} sent for printing. Number of pages: {}".format(file_path, num_pages))
    context.bot.send_message(chat_id=update.effective_chat.id, text="File sent for printing!")


def main():
    updater = Updater(token=token_key)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CommandHandler('auth', authorize))
    dispatcher.add_handler(CommandHandler('pending', pending))
    dispatcher.add_handler(CommandHandler('completed', completed))
    dispatcher.add_handler(CommandHandler('cancel', cancel))
    dispatcher.add_handler(MessageHandler(filters=Filters.document, callback=print_file))
    dispatcher.add_handler(MessageHandler(filters=Filters.photo, callback=print_file))

    logger.info("Listening...")
    updater.start_polling()

if __name__ == '__main__':
    main()

