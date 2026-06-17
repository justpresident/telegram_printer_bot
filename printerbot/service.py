"""PrinterBotService: backend-agnostic business logic."""

import os
import re
import time
import secrets
import pathlib
from typing import Optional, Tuple, List

from .domain import (
    FileType, FileInfo, printer_key,
    PrintOptions, PrintResult, PrinterStatus, JobStatus, PrinterInfo, JobState,
    Duplex, ColorMode,
)
from .interfaces import (
    PrinterInterface, FileProcessorInterface, AuthManagerInterface,
    PrinterSettingsStoreInterface,
)
from .storage import InMemoryStateStore
from .adapters import StoreBackedPrinterSettings


class PrinterBotService:
    """Core business logic for the printer bot"""

    def __init__(
        self,
        printer: PrinterInterface,
        file_processor: FileProcessorInterface,
        auth_manager: AuthManagerInterface,
        files_dir: str,
        printer_settings: Optional[PrinterSettingsStoreInterface] = None,
        file_size_limit: int = 64 * 1024 * 1024,
        max_pages_limit: int = 100
    ):
        self.printer = printer
        self.file_processor = file_processor
        self.auth_manager = auth_manager
        self.files_dir = files_dir
        self.printer_settings = printer_settings or StoreBackedPrinterSettings(InMemoryStateStore())
        self.file_size_limit = file_size_limit
        self.max_pages_limit = max_pages_limit

        # Ensure files directory exists
        pathlib.Path(files_dir).mkdir(parents=True, exist_ok=True)

    # -- printer status -----------------------------------------------------

    def get_printer_status(self) -> PrinterStatus:
        return self.printer.get_status()

    def get_pending_jobs(self) -> JobStatus:
        return self.printer.get_pending_jobs()

    def get_completed_jobs(self) -> JobStatus:
        return self.printer.get_completed_jobs()

    def list_printers(self) -> List[PrinterInfo]:
        try:
            return self.printer.list_printers()
        except Exception:
            return []

    def get_job_state(self, job_id: str) -> JobState:
        return self.printer.get_job_state(job_id)

    def cancel_job(self, job_id: str) -> Tuple[bool, str]:
        if not self._is_valid_job_id(job_id):
            return False, f"Invalid job_id '{job_id}'"

        success = self.printer.cancel_job(job_id)
        if success:
            return True, f"Job '{job_id}' cancelled successfully"
        else:
            return False, f"Failed to cancel job '{job_id}'"

    # -- auth ---------------------------------------------------------------

    def authenticate_user(self, user_id: int, password: str) -> Tuple[bool, str]:
        if self.auth_manager.is_authorized(user_id):
            return False, "You are already authorized!"

        if self.auth_manager.authorize_user(user_id, password):
            return True, "Authorization successful! Now you can print files by sending them."
        else:
            return False, "Wrong password!"

    def is_user_authorized(self, user_id: int) -> bool:
        return self.auth_manager.is_authorized(user_id)

    # -- per-printer settings -----------------------------------------------

    def get_printer_defaults(self, key: str) -> PrintOptions:
        """Saved default options for a printer (key = printer name, or "" for
        the system-default printer)."""
        return self.printer_settings.get(key)

    def save_printer_defaults(self, options: PrintOptions) -> None:
        """Persist `options` as the defaults for the printer it targets."""
        self.printer_settings.set(printer_key(options.printer), options)

    def default_printer_key(self) -> str:
        """Key of the system's default printer (CUPS default), or "" if none."""
        for info in self.list_printers():
            if info.is_default:
                return info.name
        return ""

    def seed_options(self) -> PrintOptions:
        """Default options for a brand-new job: the default printer's saved
        defaults."""
        return self.get_printer_defaults(self.default_printer_key())

    # -- files --------------------------------------------------------------

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
                # The original download is now useless — don't leak it.
                self._remove_quietly(file_path)
                return False, "Failed to convert file!", 0, ""

            # Conversion produced a new PDF; the original source is no longer needed.
            if processed_path != file_path:
                self._remove_quietly(file_path)

            # Get page count
            page_count = self.file_processor.get_page_count(processed_path)

            # Check page limit
            if page_count > self.max_pages_limit:
                # Clean up the (already converted) file
                self._remove_quietly(processed_path)
                return False, f"Too many pages ({page_count} > {self.max_pages_limit} limit)!", 0, ""

            return True, "File processed successfully", page_count, processed_path

        except Exception as e:
            return False, f"Error processing file: {str(e)}", 0, ""

    def render_preview(self, file_path: str) -> Optional[str]:
        if not self._is_valid_file_path(file_path):
            return None
        try:
            return self.file_processor.render_preview(file_path, self.files_dir)
        except Exception:
            return None

    def print_file(self, file_path: str, options: Optional[PrintOptions] = None) -> PrintResult:
        if not self._is_valid_file_path(file_path):
            return PrintResult(False, None, "File not found or invalid path")

        options = options or PrintOptions()
        try:
            page_count = self.file_processor.get_page_count(file_path)
            result = self.printer.print_file(file_path, options)

            if result.success:
                summary = self._describe_job(page_count, options)
                if options.dry_run:
                    return PrintResult(True, result.job_id, f"🧪 Dry run — command logged, nothing printed {summary}")
                return PrintResult(True, result.job_id, f"Sent to printer! {summary}")
            return PrintResult(False, None, result.message or "Failed to send file to printer")

        except Exception as e:
            return PrintResult(False, None, f"Error processing print request: {str(e)}")

    def delete_file(self, file_path: str) -> Tuple[bool, str]:
        if not self._is_valid_file_path(file_path):
            return False, "File not found or invalid path"

        try:
            os.unlink(file_path)
            return True, "File deleted"
        except Exception as e:
            return False, f"Error deleting file: {str(e)}"

    def generate_temp_filename(self, original_name: str) -> str:
        # A random, collision-resistant base name (avoids the private
        # tempfile._get_candidate_names API and name clashes in files_dir).
        _, file_extension = os.path.splitext(original_name)
        return secrets.token_hex(16) + (file_extension if file_extension else "")

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _describe_job(page_count: int, options: PrintOptions) -> str:
        pages = f"{page_count} page{'s' if page_count != 1 else ''}"
        parts = [pages]
        if options.copies > 1:
            parts.append(f"{options.copies} copies")
        if options.duplex != Duplex.ONE_SIDED:
            parts.append("double-sided")
        if options.color == ColorMode.GRAYSCALE:
            parts.append("grayscale")
        return "(" + ", ".join(parts) + ")"

    def _is_valid_job_id(self, job_id: str) -> bool:
        return bool(re.match(r'^[a-zA-Z0-9_\-]+$', job_id))

    def _is_valid_file_path(self, file_path: str) -> bool:
        try:
            abs_file_path = os.path.abspath(file_path)
            abs_files_dir = os.path.abspath(self.files_dir)

            # Containment check via commonpath so that a sibling directory
            # like "<files_dir>_evil" is not mistaken for being inside files_dir.
            if os.path.commonpath([abs_file_path, abs_files_dir]) != abs_files_dir:
                return False
            return os.path.exists(abs_file_path) and os.path.isfile(abs_file_path)
        except Exception:
            return False

    def _remove_quietly(self, path: str):
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass

    def cleanup_stale_files(self, max_age_seconds: int = 24 * 3600) -> int:
        """Delete files in files_dir older than max_age_seconds. Bounds disk
        usage from jobs that were uploaded but never printed/deleted (e.g. the
        user abandoned the panel, or the in-memory job registry was lost on a
        restart). Returns the number of files removed."""
        removed = 0
        now = time.time()
        try:
            entries = os.scandir(self.files_dir)
        except OSError:
            return 0
        with entries:
            for entry in entries:
                try:
                    if entry.is_file() and (now - entry.stat().st_mtime) > max_age_seconds:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
        return removed


