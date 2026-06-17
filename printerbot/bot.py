"""TelegramPrinterBot: all Telegram I/O and handler wiring."""

import os
import re
import logging
import secrets
import asyncio
from dataclasses import replace
from typing import Optional, Dict, Any, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackContext, ContextTypes,
    CallbackQueryHandler,
)
from telegram.ext import filters

from .domain import FileType, FileInfo, PrintResult, JobPhase
from .service import PrinterBotService
from .ui import (
    build_options_keyboard, build_submenu_keyboard,
    apply_option_action, apply_field_choice, fenced_block,
    BOT_COMMANDS, SCOPE_JOB, SCOPE_SETTINGS,
)


def _printer_display(key: str) -> str:
    """Human label for a printer settings key ("" is the system default)."""
    return key if key else "System default"


def _printer_key_for_index(index: int, printer_names: List[str]) -> str:
    """Map a printer sub-menu index to its settings key. Index 0 is always the
    system default ("")."""
    if 1 <= index <= len(printer_names):
        return printer_names[index - 1]
    return ""


class TelegramPrinterBot:
    """Telegram bot wrapper around PrinterBotService"""

    # how long live job-status polling runs: STATUS_POLLS * STATUS_INTERVAL secs
    STATUS_POLLS = 40
    STATUS_INTERVAL = 3
    # how often the background sweep removes stale files from files_dir
    CLEANUP_INTERVAL = 3600

    def __init__(self, token: str, service: PrinterBotService, logger: logging.Logger):
        self.token = token
        self.service = service
        self.logger = logger
        self.application = None

    def create_application(self) -> Application:
        self.application = (
            Application.builder()
            .token(self.token)
            .post_init(self._post_init)
            .build()
        )

        # Commands
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("auth", self.authorize))
        self.application.add_handler(CommandHandler("pending", self.pending))
        self.application.add_handler(CommandHandler("completed", self.completed))
        self.application.add_handler(CommandHandler("cancel", self.cancel))
        self.application.add_handler(CommandHandler("settings", self.settings))

        # Files
        self.application.add_handler(
            MessageHandler(filters.Document.ALL | filters.PHOTO, self.upload_file)
        )
        self.application.add_handler(CallbackQueryHandler(self.button))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_callback)
        )

        return self.application

    async def _post_init(self, application: Application) -> None:
        try:
            await application.bot.set_my_commands(
                [BotCommand(name, desc) for name, desc in BOT_COMMANDS]
            )
        except Exception as e:
            self.logger.error(f"Failed to set command menu: {e}")
        # Periodically sweep abandoned files (the startup sweep in main() only
        # runs once; this bounds disk use during a long-lived session).
        application.create_task(self._periodic_cleanup())

    async def _periodic_cleanup(self) -> None:
        while True:
            await asyncio.sleep(self.CLEANUP_INTERVAL)
            try:
                removed = await self._offload(self.service.cleanup_stale_files)
                if removed:
                    self.logger.info(f"Periodic cleanup removed {removed} stale file(s)")
            except Exception as e:
                self.logger.error(f"Periodic cleanup failed: {e}")

    def run(self):
        if not self.application:
            self.create_application()

        self.logger.info("🤖 Telegram Printer Bot starting...")
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

    # -- helpers ------------------------------------------------------------

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

    async def _update_message(self, context: CallbackContext, msg, text: str):
        try:
            await context.bot.edit_message_text(text, msg.chat.id, msg.message_id)
        except Exception as e:
            self.logger.error(f"Error updating message: {e}")

    async def _edit_panel(self, query, text: str, reply_markup=None):
        """Edit a panel message whether it is a text message or a photo (caption)."""
        try:
            if query.message and query.message.photo:
                await query.edit_message_caption(caption=text, reply_markup=reply_markup)
            else:
                await query.edit_message_text(text=text, reply_markup=reply_markup)
        except Exception as e:
            self.logger.error(f"Error editing panel: {e}")

    def _job_registry(self, context: CallbackContext) -> Dict[str, Any]:
        return context.user_data.setdefault("jobs", {})

    @staticmethod
    async def _offload(func, *args):
        """Run a blocking (subprocess-bound) service call off the event loop so
        the single asyncio loop stays responsive during conversions/printing."""
        return await asyncio.to_thread(func, *args)

    async def _printer_names(self) -> List[str]:
        printers = await self._offload(self.service.list_printers)
        return [p.name for p in printers]

    # -- command handlers ---------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) /start request")

        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        status = await self._offload(self.service.get_printer_status)
        if status.error:
            await update.message.reply_text("🖨️ You are authorized to print! Just send a file here.")
        else:
            msg = "🖨️ You are authorized to print! Just send a file here.\n\n"
            msg += f"📊 Current printer status:\n{fenced_block(status.status)}\n\n"
            msg += f"📋 Printer queue:\n{fenced_block(status.queue)}"
            await update.message.reply_text(msg, parse_mode='Markdown')

    async def pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) /pending request")

        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        jobs = await self._offload(self.service.get_pending_jobs)
        if jobs.error:
            await update.message.reply_text("❌ Error checking pending jobs")
        else:
            msg = "✅ No pending jobs found" if not jobs.jobs else f"⏳ Pending jobs:\n{fenced_block(jobs.jobs)}"
            await update.message.reply_text(msg, parse_mode='Markdown')

    async def completed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) /completed request")

        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        jobs = await self._offload(self.service.get_completed_jobs)
        if jobs.error:
            await update.message.reply_text("❌ Error checking completed jobs")
        else:
            msg = "📋 No completed jobs found" if not jobs.jobs else f"✅ Recent completed jobs:\n{fenced_block(jobs.jobs)}"
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

        # A job id is a single token (e.g. "Office-42"); use the first arg only.
        job_id = context.args[0].strip()
        success, message = await self._offload(self.service.cancel_job, job_id)

        if success:
            await update.message.reply_text(f"✅ {message}")
        else:
            await update.message.reply_text(f"❌ {message}")

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self._get_user_id(update)
        username = self._get_username(update)
        self.logger.info(f"User {username} (ID: {user_id}) /settings request")

        if not self.service.is_user_authorized(user_id):
            return await self._request_auth(update, context)

        printers = await self._offload(self.service.list_printers)
        printer_names = [p.name for p in printers]
        key = next((p.name for p in printers if p.is_default), "")
        context.user_data["settings_printer"] = key
        options = self.service.get_printer_defaults(key)
        keyboard = build_options_keyboard(options, SCOPE_SETTINGS, "_", printer_names)
        await update.message.reply_text(
            f"⚙️ Default print settings for *{_printer_display(key)}*.\n"
            "These apply to new files sent to this printer; pick a different "
            "printer below to edit its defaults.",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

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

        # If we're waiting for a typed page range, consume this message as the range.
        if context.user_data.get("awaiting_range"):
            return await self._handle_range_input(update, context)

        await update.message.reply_text("📄 Please send a document or image file to print.\n\n"
                                      "Available commands:\n"
                                      "• `/start` - Show printer status\n"
                                      "• `/settings` - Default print settings\n"
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
            success, message, page_count, processed_path = await self._offload(self.service.process_file, file_path)

            if not success:
                await self._update_message(context, reply_msg, f"❌ {message}")
                return

            # Delete status message
            try:
                await context.bot.delete_message(reply_msg.chat.id, reply_msg.message_id)
            except Exception:
                pass

            await self._present_print_panel(update, context, user_id, processed_path, page_count)

        except Exception as e:
            self.logger.error(f"Error processing file: {e}")
            await self._update_message(context, reply_msg, f"❌ Error processing file: {str(e)}")

    async def _present_print_panel(self, update, context, user_id, processed_path, page_count):
        """Register the job and show the interactive options panel (with preview)."""
        printers = await self._offload(self.service.list_printers)
        printer_names = [p.name for p in printers]
        default_key = next((p.name for p in printers if p.is_default), "")
        options = self.service.get_printer_defaults(default_key)

        token = secrets.token_hex(4)
        self._job_registry(context)[token] = {
            "file_path": processed_path,
            "page_count": page_count,
            "options": options,
            "printers": printer_names,
        }

        keyboard = build_options_keyboard(options, SCOPE_JOB, token, printer_names)
        page_text = f"{page_count} page{'s' if page_count != 1 else ''}"
        caption = f"📄 Ready to print: {page_text}\nChoose options, then press Print."

        preview_path = await self._offload(self.service.render_preview, processed_path)
        if preview_path:
            try:
                with open(preview_path, "rb") as preview:
                    await update.message.reply_photo(preview, caption=caption, reply_markup=keyboard)
                return
            except Exception as e:
                self.logger.error(f"Error sending preview: {e}")
            finally:
                try:
                    os.unlink(preview_path)
                except OSError:
                    pass

        await update.message.reply_text(caption, reply_markup=keyboard)

    # -- callback routing ---------------------------------------------------

    async def button(self, update: Update, context: CallbackContext):
        query = update.callback_query
        user_id = self._get_user_id(update)
        username = self._get_username(update)

        self.logger.info(f"User {username} (ID: {user_id}) clicked button: {query.data}")

        if not self.service.is_user_authorized(user_id):
            await query.answer("❌ Not authorized")
            return

        await query.answer()

        verb, _, rest = query.data.partition(" ")

        if verb == "noop":
            return
        if verb == "cancel":
            return await self._handle_cancel_button(query, rest.strip())

        scope, _, key = rest.partition(":")
        if scope not in (SCOPE_JOB, SCOPE_SETTINGS):
            await self._edit_panel(query, "❌ Invalid button data")
            return

        await self._handle_panel_button(query, context, verb, scope, key, user_id)

    async def _handle_cancel_button(self, query, job_id: str):
        success, message = await self._offload(self.service.cancel_job, job_id)
        emoji = "🚫" if success else "❌"
        await self._edit_panel(query, f"{emoji} {message}")

    async def _handle_panel_button(self, query, context, verb, scope, key, user_id):
        """Single router for the per-job ('j') and settings ('s') option panels:
        sub-menu open/back/set, the copies & dry-run controls, range entry, and
        the job-only Print/Delete and settings-only Done actions."""
        # Resolve where the current options live and how to persist a change.
        # Job options live in the registry (one-off); settings options are the
        # saved defaults of the printer currently shown in the panel.
        entry = None
        if scope == SCOPE_JOB:
            entry = self._job_registry(context).get(key)
            if not entry:
                await self._edit_panel(query, "⌛ This file is no longer available. Please re-send it.")
                return
            options = entry["options"]
            printer_names = entry.get("printers") or []
        else:
            settings_key = context.user_data.get("settings_printer", "")
            options = self.service.get_printer_defaults(settings_key)
            printer_names = await self._printer_names()

        def persist(new_options):
            if entry is not None:
                entry["options"] = new_options
            else:
                self.service.save_printer_defaults(new_options)

        # Terminal actions.
        if scope == SCOPE_JOB and verb == "print":
            result = await self._offload(self.service.print_file, entry["file_path"], options)
            self._job_registry(context).pop(key, None)
            if not result.success:
                await self._edit_panel(query, f"❌ {result.message}")
            else:
                await self._start_live_status(query, context, result, entry["page_count"])
            return
        if scope == SCOPE_JOB and verb == "delete":
            success, message = await self._offload(self.service.delete_file, entry["file_path"])
            self._job_registry(context).pop(key, None)
            await self._edit_panel(query, f"{'🗑️' if success else '❌'} {message}")
            return
        if scope == SCOPE_SETTINGS and verb == "done":
            await self._edit_panel(query, "✅ Settings saved. They'll apply to every new file.")
            return

        if verb == "range":
            await self._prompt_for_range(query, context, scope, key)
            return

        # Sub-menu navigation.
        if verb.startswith("open:"):
            field = verb.split(":", 1)[1]
            await self._render_panel(query, build_submenu_keyboard(field, options, scope, key, printer_names))
            return
        if verb == "back":
            await self._render_panel(query, build_options_keyboard(options, scope, key, printer_names))
            return
        if verb.startswith("set:"):
            parts = verb.split(":", 2)  # set:<field>:<index>
            if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
                return
            field, index = parts[1], int(parts[2])
            if field == "printer":
                new_options = self._select_printer(context, entry, scope, options, index, printer_names)
            else:
                new_options = apply_field_choice(options, field, index, printer_names)
                persist(new_options)
            await self._render_panel(query, build_options_keyboard(new_options, scope, key, printer_names))
            return

        # Simple controls (copies stepper, dry-run toggle).
        new_options = apply_option_action(options, verb)
        persist(new_options)
        await self._render_panel(query, build_options_keyboard(new_options, scope, key, printer_names))

    def _select_printer(self, context, entry, scope, options, index, printer_names):
        """Pick a printer from its sub-menu. Selecting a printer loads that
        printer's saved defaults (the point of per-printer settings)."""
        chosen_key = _printer_key_for_index(index, printer_names)
        defaults = self.service.get_printer_defaults(chosen_key)
        if scope == SCOPE_JOB:
            # Keep the document-specific choices when switching printer.
            new_options = replace(defaults, copies=options.copies, page_ranges=options.page_ranges)
            entry["options"] = new_options
            return new_options
        # Settings: switch which printer we're editing (no save until changed).
        context.user_data["settings_printer"] = chosen_key
        return defaults

    async def _render_panel(self, query, keyboard):
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception as e:
            self.logger.error(f"Error rendering panel: {e}")

    # -- page-range typed input ---------------------------------------------

    async def _prompt_for_range(self, query, context, scope, key):
        if not query.message:
            # Message too old / inaccessible — nothing to anchor the panel edit to.
            return
        context.user_data["awaiting_range"] = {
            "scope": scope,
            "key": key,
            "chat_id": query.message.chat.id,
            "message_id": query.message.message_id,
        }
        await context.bot.send_message(
            query.message.chat.id,
            "📑 Send the page range to print, e.g. `2-5` or `1,3,5` — or `all` for every page.",
            parse_mode="Markdown",
        )

    async def _handle_range_input(self, update: Update, context: CallbackContext):
        raw = (update.message.text or "").strip()

        if raw.lower() in ("all", "*", ""):
            page_ranges = ""
        elif re.match(r'^[0-9]+(?:-[0-9]+)?(?:\s*,\s*[0-9]+(?:-[0-9]+)?)*$', raw):
            page_ranges = re.sub(r'\s+', '', raw)
        else:
            # Keep awaiting_range set so the user's next message retries the range.
            await update.message.reply_text("❌ Invalid range. Use e.g. `2-5` or `1,3,5` or `all`.", parse_mode="Markdown")
            return

        pending = context.user_data.pop("awaiting_range")
        scope, key = pending["scope"], pending["key"]

        if scope == SCOPE_JOB:
            entry = self._job_registry(context).get(key)
            if not entry:
                await update.message.reply_text("⌛ This file is no longer available. Please re-send it.")
                return
            entry["options"] = replace(entry["options"], page_ranges=page_ranges)
            options, printer_names = entry["options"], entry.get("printers")
        else:
            settings_key = context.user_data.get("settings_printer", "")
            options = replace(self.service.get_printer_defaults(settings_key), page_ranges=page_ranges)
            self.service.save_printer_defaults(options)
            printer_names = await self._printer_names()

        # Re-render the original panel's keyboard in place.
        keyboard = build_options_keyboard(options, scope, key, printer_names)
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=pending["chat_id"],
                message_id=pending["message_id"],
                reply_markup=keyboard,
            )
        except Exception as e:
            self.logger.error(f"Error updating panel after range input: {e}")

        label = "all pages" if not page_ranges else f"pages {page_ranges}"
        await update.message.reply_text(f"✅ Will print {label}.")

    # -- live job status ----------------------------------------------------

    async def _start_live_status(self, query, context, result: PrintResult, page_count):
        if not result.job_id:
            await self._edit_panel(query, f"🖨️ {result.message}")
            return

        cancel_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚫 Cancel", callback_data=f"cancel {result.job_id}")]]
        )
        await self._edit_panel(query, f"🖨️ {result.message}\nJob `{result.job_id}` queued…", cancel_kb)

        # Background polling needs a concrete message to edit; skip it if the
        # callback message is inaccessible (e.g. too old).
        if not query.message:
            return
        context.application.create_task(
            self._poll_job_status(
                context, query.message.chat.id, query.message.message_id,
                result.job_id, page_count, bool(query.message.photo),
            )
        )

    # consecutive UNKNOWN polls (before the job is ever seen active) after which
    # we assume a fast job already printed and was purged, and finalize.
    UNKNOWN_GRACE_POLLS = 3

    async def _poll_job_status(self, context, chat_id, message_id, job_id, page_count, is_photo):
        seen_active = False
        unknown_polls = 0
        cancel_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚫 Cancel", callback_data=f"cancel {job_id}")]]
        )
        try:
            for _ in range(self.STATUS_POLLS):
                await asyncio.sleep(self.STATUS_INTERVAL)
                state = await self._offload(self.service.get_job_state, job_id)

                if state.phase == JobPhase.COMPLETED:
                    await self._edit_by_id(context, chat_id, message_id, is_photo,
                                           f"✅ Printed {page_count} page(s) — job {job_id}")
                    return
                if state.phase in (JobPhase.PROCESSING, JobPhase.PENDING):
                    unknown_polls = 0
                    if not seen_active:
                        seen_active = True
                        await self._edit_by_id(context, chat_id, message_id, is_photo,
                                               f"🖨️ Printing… job {job_id}", cancel_kb)
                else:  # UNKNOWN — no longer in any CUPS queue
                    if seen_active:
                        await self._edit_by_id(context, chat_id, message_id, is_photo,
                                               f"✅ Finished — job {job_id}")
                        return
                    # Never seen active: a very fast job may have printed and been
                    # purged before our first poll. Finalize after a short grace
                    # rather than leaving the user staring at "queued…".
                    unknown_polls += 1
                    if unknown_polls >= self.UNKNOWN_GRACE_POLLS:
                        await self._edit_by_id(context, chat_id, message_id, is_photo,
                                               f"✅ Finished — job {job_id}")
                        return
        except Exception as e:
            self.logger.error(f"Error polling job status: {e}")

    async def _edit_by_id(self, context, chat_id, message_id, is_photo, text, reply_markup=None):
        try:
            if is_photo:
                await context.bot.edit_message_caption(
                    chat_id=chat_id, message_id=message_id, caption=text, reply_markup=reply_markup)
            else:
                await context.bot.edit_message_text(
                    text, chat_id, message_id, reply_markup=reply_markup)
        except Exception as e:
            self.logger.debug(f"Status edit skipped: {e}")

    # -- file extraction ----------------------------------------------------

    def _extract_file_info(self, message) -> Optional[FileInfo]:
        if message.document:
            return FileInfo(
                file_id=message.document.file_id,
                file_size=message.document.file_size or 0,  # Telegram may omit size
                file_name=message.document.file_name or "document",
                file_type=FileType.DOCUMENT
            )
        elif message.photo:
            photo = message.photo[-1]  # Get highest resolution
            return FileInfo(
                file_id=photo.file_id,
                file_size=photo.file_size or 0,  # Telegram may omit size
                file_name=f"photo_{photo.file_unique_id}.jpg",
                file_type=FileType.PHOTO
            )
        return None


