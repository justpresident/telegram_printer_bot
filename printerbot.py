#!/usr/bin/python3

import logging
import os
import pathlib
import re
import tempfile
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple, Set, Dict, Any
from enum import Enum

import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, ContextTypes, CallbackQueryHandler
from telegram.ext import filters


class FileType(Enum):
    DOCUMENT = "document"
    PHOTO = "photo"
    UNKNOWN = "unknown"


@dataclass
class FileInfo:
    file_id: str
    file_size: int
    file_name: str
    file_type: FileType


@dataclass
class PrinterStatus:
    status: str
    queue: str
    error: Optional[str] = None


@dataclass
class JobStatus:
    jobs: str
    error: Optional[str] = None


class PrinterInterface(ABC):
    """Abstract interface for printer operations"""

    @abstractmethod
    def get_status(self) -> PrinterStatus:
        pass

    @abstractmethod
    def get_pending_jobs(self) -> JobStatus:
        pass

    @abstractmethod
    def get_completed_jobs(self) -> JobStatus:
        pass

    @abstractmethod
    def cancel_job(self, job_id: str) -> bool:
        pass

    @abstractmethod
    def print_file(self, file_path: str) -> bool:
        pass


class FileProcessorInterface(ABC):
    """Abstract interface for file processing operations"""

    @abstractmethod
    def convert_to_pdf(self, file_path: str, output_dir: str) -> Tuple[str, bool]:
        pass

    @abstractmethod
    def get_page_count(self, file_path: str) -> int:
        pass

    @abstractmethod
    def is_pdf(self, file_path: str) -> bool:
        pass


class AuthManagerInterface(ABC):
    """Abstract interface for authentication management"""

    @abstractmethod
    def is_authorized(self, user_id: int) -> bool:
        pass

    @abstractmethod
    def authorize_user(self, user_id: int, password: str) -> bool:
        pass

    @abstractmethod
    def get_correct_password(self) -> str:
        pass


class SystemPrinter(PrinterInterface):
    """System printer implementation using CUPS commands"""

    def get_status(self) -> PrinterStatus:
        try:
            status = os.popen('lpstat -p').read().strip()
            queue = os.popen('lpq').read().strip()
            return PrinterStatus(status=status, queue=queue)
        except Exception as e:
            return PrinterStatus(status="", queue="", error=str(e))

    def get_pending_jobs(self) -> JobStatus:
        try:
            jobs = os.popen('lpstat -W not-completed').read().strip()
            return JobStatus(jobs=jobs)
        except Exception as e:
            return JobStatus(jobs="", error=str(e))

    def get_completed_jobs(self) -> JobStatus:
        try:
            jobs = os.popen('lpstat -W completed | head').read().strip()
            return JobStatus(jobs=jobs)
        except Exception as e:
            return JobStatus(jobs="", error=str(e))

    def cancel_job(self, job_id: str) -> bool:
        try:
            result = os.system(f"cancel {job_id}")
            return result == 0
        except Exception:
            return False

    def print_file(self, file_path: str) -> bool:
        try:
            result = os.system(f'lpr "{file_path}"')
            return result == 0
        except Exception:
            return False


class LibreOfficeFileProcessor(FileProcessorInterface):
    """File processor using LibreOffice for conversions"""

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    def convert_to_pdf(self, file_path: str, output_dir: str) -> Tuple[str, bool]:
        if self.is_pdf(file_path):
            return file_path, True

        try:
            abs_file_path = os.path.abspath(file_path)
            abs_output_dir = os.path.abspath(output_dir)

            cmd = f'timeout {self.timeout} libreoffice --headless --convert-to pdf "{abs_file_path}" --outdir "{abs_output_dir}"'
            result = os.system(cmd)

            if result == 0:
                base_name = os.path.splitext(os.path.basename(file_path))[0]
                new_path = os.path.join(output_dir, base_name + '.pdf')
                if os.path.exists(new_path):
                    return new_path, True

            return file_path, False
        except Exception:
            return file_path, False

    def get_page_count(self, file_path: str) -> int:
        try:
            output = os.popen(f'pdfinfo "{file_path}" | grep Pages').read().strip()
            if output:
                return int(''.join(filter(str.isdigit, output)))
            return 0
        except Exception:
            return 0

    def is_pdf(self, file_path: str) -> bool:
        _, ext = os.path.splitext(file_path)
        return ext.lower() == ".pdf"


class InMemoryAuthManager(AuthManagerInterface):
    """In-memory authentication manager"""

    def __init__(self, correct_password: str):
        self.correct_password = correct_password
        self.authorized_users: Set[int] = set()

    def is_authorized(self, user_id: int) -> bool:
        return user_id in self.authorized_users

    def authorize_user(self, user_id: int, password: str) -> bool:
        if password == self.correct_password:
            self.authorized_users.add(user_id)
            return True
        return False

    def get_correct_password(self) -> str:
        return self.correct_password


class FileAuthManager(AuthManagerInterface):
    """File-based authentication manager"""

    def __init__(self, password_file: str):
        self.password_file = password_file
        self.authorized_users: Set[int] = set()
        self._load_password()

    def _load_password(self):
        try:
            with open(self.password_file, 'r') as f:
                self.correct_password = f.read().strip()
        except Exception as e:
            raise ValueError(f"Cannot read password file {self.password_file}: {e}")

    def is_authorized(self, user_id: int) -> bool:
        return user_id in self.authorized_users

    def authorize_user(self, user_id: int, password: str) -> bool:
        if password == self.correct_password:
            self.authorized_users.add(user_id)
            return True
        return False

    def get_correct_password(self) -> str:
        return self.correct_password


class PrinterBotService:
    """Core business logic for the printer bot"""

    def __init__(
        self,
        printer: PrinterInterface,
        file_processor: FileProcessorInterface,
        auth_manager: AuthManagerInterface,
        files_dir: str,
        file_size_limit: int = 64 * 1024 * 1024,
        max_pages_limit: int = 100
    ):
        self.printer = printer
        self.file_processor = file_processor
        self.auth_manager = auth_manager
        self.files_dir = files_dir
        self.file_size_limit = file_size_limit
        self.max_pages_limit = max_pages_limit

        # Ensure files directory exists
        pathlib.Path(files_dir).mkdir(parents=True, exist_ok=True)

    def get_printer_status(self) -> PrinterStatus:
        return self.printer.get_status()

    def get_pending_jobs(self) -> JobStatus:
        return self.printer.get_pending_jobs()

    def get_completed_jobs(self) -> JobStatus:
        return self.printer.get_completed_jobs()

    def cancel_job(self, job_id: str) -> Tuple[bool, str]:
        if not self._is_valid_job_id(job_id):
            return False, f"Invalid job_id '{job_id}'"

        success = self.printer.cancel_job(job_id)
        if success:
            return True, f"Job '{job_id}' cancelled successfully"
        else:
            return False, f"Failed to cancel job '{job_id}'"

    def authenticate_user(self, user_id: int, password: str) -> Tuple[bool, str]:
        if self.auth_manager.is_authorized(user_id):
            return False, "You are already authorized!"

        if self.auth_manager.authorize_user(user_id, password):
            return True, "Authorization successful! Now you can print files by sending them."
        else:
            return False, "Wrong password!"

    def is_user_authorized(self, user_id: int) -> bool:
        return self.auth_manager.is_authorized(user_id)

    def validate_file(self, file_info: FileInfo) -> Tuple[bool, str]:
        if file_info.file_size > self.file_size_limit:
            return False, f"File is too large ({file_info.file_size:,} bytes > {self.file_size_limit:,} bytes limit)!"

        if file_info.file_type == FileType.UNKNOWN:
            return False, "Unsupported file type. Please send a document or photo."

        return True, "File is valid"

    def process_file(self, file_path: str) -> Tuple[bool, str, int, str]:
        """
        Process file for printing
        Returns: (success, message, page_count, processed_file_path)
        """
        try:
            # Convert to PDF if needed
            processed_path, conversion_success = self.file_processor.convert_to_pdf(
                file_path, self.files_dir
            )

            if not conversion_success:
                return False, "Failed to convert file!", 0, ""

            # Get page count
            page_count = self.file_processor.get_page_count(processed_path)

            # Check page limit
            if page_count > self.max_pages_limit:
                # Clean up file
                self._cleanup_file(processed_path, file_path)
                return False, f"Too many pages ({page_count} > {self.max_pages_limit} limit)!", 0, ""

            return True, "File processed successfully", page_count, processed_path

        except Exception as e:
            return False, f"Error processing file: {str(e)}", 0, ""

    def print_file(self, file_path: str) -> Tuple[bool, str]:
        if not self._is_valid_file_path(file_path):
            return False, "File not found or invalid path"

        try:
            page_count = self.file_processor.get_page_count(file_path)
            success = self.printer.print_file(file_path)

            if success:
                return True, f"File sent to printer! ({page_count} pages)"
            else:
                return False, "Failed to send file to printer"

        except Exception as e:
            return False, f"Error processing print request: {str(e)}"

    def delete_file(self, file_path: str) -> Tuple[bool, str]:
        if not self._is_valid_file_path(file_path):
            return False, "File not found or invalid path"

        try:
            os.unlink(file_path)
            return True, "File deleted"
        except Exception as e:
            return False, f"Error deleting file: {str(e)}"

    def generate_temp_filename(self, original_name: str) -> str:
        temp_name = next(tempfile._get_candidate_names())
        _, file_extension = os.path.splitext(original_name)
        return temp_name + (file_extension if file_extension else "")

    def _is_valid_job_id(self, job_id: str) -> bool:
        return bool(re.match(r'^[a-zA-Z0-9_\-]+$', job_id))

    def _is_valid_file_path(self, file_path: str) -> bool:
        try:
            abs_file_path = os.path.abspath(file_path)
            abs_files_dir = os.path.abspath(self.files_dir)

            return (abs_file_path.startswith(abs_files_dir) and
                    os.path.exists(abs_file_path) and
                    os.path.isfile(abs_file_path))
        except:
            return False

    def _cleanup_file(self, processed_path: str, original_path: str):
        try:
            if os.path.exists(processed_path):
                os.unlink(processed_path)
            if processed_path != original_path and os.path.exists(original_path):
                os.unlink(original_path)
        except:
            pass


class TelegramPrinterBot:
    """Telegram bot wrapper around PrinterBotService"""

    def __init__(self, token: str, service: PrinterBotService, logger: logging.Logger):
        self.token = token
        self.service = service
        self.logger = logger
        self.application = None

    def create_application(self) -> Application:
        self.application = Application.builder().token(self.token).build()

        # Add handlers
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("auth", self.authorize))
        self.application.add_handler(CommandHandler("pending", self.pending))
        self.application.add_handler(CommandHandler("completed", self.completed))
        self.application.add_handler(CommandHandler("cancel", self.cancel))

        # File handlers
        self.application.add_handler(
            MessageHandler(filters.Document.ALL | filters.PHOTO, self.upload_file)
        )
        self.application.add_handler(CallbackQueryHandler(self.button))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_callback)
        )

        return self.application

    def run(self):
        if not self.application:
            self.create_application()

        self.logger.info("🤖 Telegram Printer Bot starting...")
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

    def _get_user_id(self, update: Update) -> Optional[int]:
        if update.message:
            return update.message.from_user.id
        elif update.callback_query:
            return update.callback_query.from_user.id
        return None

    def _get_username(self, update: Update) -> str:
        user_id = self._get_user_id(update)
        if update.message and update.message.from_user.username:
            return update.message.from_user.username
        elif update.callback_query and update.callback_query.from_user.username:
            return update.callback_query.from_user.username
        return f"user_{user_id}"

    async def _request_auth(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Please authorize by \"/auth <password>\".")

    async def _update_message(self, context: CallbackContext, msg: telegram.Message, text: str):
        try:
            await context.bot.edit_message_text(text, msg.chat.id, msg.message_id)
        except Exception as e:
            self.logger.error(f"Error updating message: {e}")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) /start request")

        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        status = self.service.get_printer_status()
        if status.error:
            await update.message.reply_text("🖨️ You are authorized to print! Just send a file here.")
        else:
            msg = "🖨️ You are authorized to print! Just send a file here.\n\n"
            msg += f"📊 Current printer status:\n```\n{status.status}\n```\n\n"
            msg += f"📋 Printer queue:\n```\n{status.queue}\n```"
            await update.message.reply_text(msg, parse_mode='Markdown')

    async def pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) /pending request")

        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        jobs = self.service.get_pending_jobs()
        if jobs.error:
            await update.message.reply_text("❌ Error checking pending jobs")
        else:
            msg = "✅ No pending jobs found" if not jobs.jobs else f"⏳ Pending jobs:\n```\n{jobs.jobs}\n```"
            await update.message.reply_text(msg, parse_mode='Markdown')

    async def completed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) /completed request")

        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        jobs = self.service.get_completed_jobs()
        if jobs.error:
            await update.message.reply_text("❌ Error checking completed jobs")
        else:
            msg = "📋 No completed jobs found" if not jobs.jobs else f"✅ Recent completed jobs:\n```\n{jobs.jobs}\n```"
            await update.message.reply_text(msg, parse_mode='Markdown')

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) /cancel request")

        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        if not context.args:
            await update.message.reply_text("❌ Please provide a job ID: `/cancel <job_id>`", parse_mode='Markdown')
            return

        job_id = ''.join(context.args).strip()
        success, message = self.service.cancel_job(job_id)

        if success:
            await update.message.reply_text(f"✅ {message}")
        else:
            await update.message.reply_text(f"❌ {message}")

    async def authorize(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) /authorize request")

        if not context.args:
            await update.message.reply_text("❌ Please provide password: `/auth <password>`", parse_mode='Markdown')
            return

        password = ' '.join(context.args)
        success, message = self.service.authenticate_user(user_id, password)

        if success:
            self.logger.info(f"User {username} (ID: {user_id}) authorized.")
            await update.message.reply_text(f"🎉 {message}")
        else:
            if "already authorized" not in message:
                self.logger.info(f"User {username} (ID: {user_id}) entered wrong password.")
            await update.message.reply_text(f"{'✅' if 'already' in message else '❌'} {message}")

    async def text_callback(self, update: Update, context: CallbackContext):
        user_id = self._get_user_id(update)
        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        await update.message.reply_text("📄 Please send a document or image file to print.\n\n"
                                      "Available commands:\n"
                                      "• `/start` - Show printer status\n"
                                      "• `/pending` - Show pending jobs\n"
                                      "• `/completed` - Show completed jobs\n"
                                      "• `/cancel <job_id>` - Cancel a print job")

    async def upload_file(self, update: Update, context: CallbackContext):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) file upload")

        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        # Extract file info
        file_info = self._extract_file_info(update.message)
        if not file_info:
            await update.message.reply_text("❌ Unsupported file type. Please send a document or photo.")
            return

        # Validate file
        valid, message = self.service.validate_file(file_info)
        if not valid:
            await update.message.reply_text(f"❌ {message}")
            return

        reply_msg = await update.message.reply_text("⬇️ Downloading file...")

        try:
            # Download file
            temp_name = self.service.generate_temp_filename(file_info.file_name)
            file_path = os.path.join(self.service.files_dir, temp_name)

            # Get file object and download
            if update.message.document:
                new_file = await update.message.document.get_file()
            else:  # photo
                new_file = await update.message.photo[-1].get_file()

            await new_file.download_to_drive(custom_path=file_path)
            self.logger.info(f"Downloaded file '{file_info.file_name}' as '{temp_name}'")

            # Process file
            await self._update_message(context, reply_msg, "🔄 Processing file...")
            success, message, page_count, processed_path = self.service.process_file(file_path)

            if not success:
                await self._update_message(context, reply_msg, f"❌ {message}")
                return

            # Delete status message
            try:
                await context.bot.delete_message(reply_msg.chat.id, reply_msg.message_id)
            except:
                pass

            # Create response with buttons
            keyboard = [
                [telegram.InlineKeyboardButton("🖨️ Print", callback_data=f"print {processed_path}")],
                [telegram.InlineKeyboardButton("🗑️ Delete", callback_data=f"delete {processed_path}")],
            ]
            reply_markup = telegram.InlineKeyboardMarkup(keyboard)

            page_text = f"{page_count} page{'s' if page_count != 1 else ''}"
            await update.message.reply_text(f"📄 Ready to print: {page_text}", reply_markup=reply_markup)

        except Exception as e:
            self.logger.error(f"Error processing file: {e}")
            await self._update_message(context, reply_msg, f"❌ Error processing file: {str(e)}")

    async def button(self, update: Update, context: CallbackContext):
        query = update.callback_query
        user_id = self._get_user_id(update)
        username = self._get_username(update)

        self.logger.info(f"User {username} (ID: {user_id}) clicked button: {query.data}")

        if not self.service.is_user_authorized(user_id):
            await query.answer("❌ Not authorized")
            return

        await query.answer()

        try:
            cmd, file_path = query.data.split(' ', 1)
        except ValueError:
            await self._update_message(context, query.message, "❌ Invalid button data")
            return

        if cmd == 'delete':
            success, message = self.service.delete_file(file_path)
            emoji = "🗑️" if success else "❌"
            await self._update_message(context, query.message, f"{emoji} {message}")
        elif cmd == 'print':
            success, message = self.service.print_file(file_path)
            emoji = "🖨️" if success else "❌"
            await self._update_message(context, query.message, f"{emoji} {message}")

            if success:
                self.logger.info(f"Successfully sent {file_path} to printer for user {username} (ID: {user_id})")
        else:
            await self._update_message(context, query.message, "❌ Unknown command")

    def _extract_file_info(self, message) -> Optional[FileInfo]:
        if message.document:
            return FileInfo(
                file_id=message.document.file_id,
                file_size=message.document.file_size,
                file_name=message.document.file_name or "document",
                file_type=FileType.DOCUMENT
            )
        elif message.photo:
            photo = message.photo[-1]  # Get highest resolution
            return FileInfo(
                file_id=photo.file_id,
                file_size=photo.file_size,
                file_name=f"photo_{photo.file_unique_id}.jpg",
                file_type=FileType.PHOTO
            )
        return None


def setup_logging() -> logging.Logger:
    """Setup logging configuration"""
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler("printerbot.log")
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    logger.addHandler(console_handler)

    return logger


def main() -> None:
    """Main function"""
    # Setup paths
    token_path = "./token"
    password_path = "./auth_password"
    files_dir = "printed_files"

    try:
        # Read configuration
        with open(token_path, "r") as f:
            token = f.read().strip()

        # Setup logging
        logger = setup_logging()

        # Create service components
        printer = SystemPrinter()
        file_processor = LibreOfficeFileProcessor()
        auth_manager = FileAuthManager(password_path)

        # Create service
        service = PrinterBotService(
            printer=printer,
            file_processor=file_processor,
            auth_manager=auth_manager,
            files_dir=files_dir,
            file_size_limit=64 * 1024 * 1024,
            max_pages_limit=100
        )

        # Create and run bot
        bot = TelegramPrinterBot(token, service, logger)
        bot.run()

    except Exception as e:
        logging.error(f"Fatal error starting bot: {e}")
        raise


if __name__ == '__main__':
    main()
