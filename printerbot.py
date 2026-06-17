#!/usr/bin/python3

import logging
import os
import json
import pathlib
import re
import secrets
import shlex
import subprocess
import time
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Optional, Tuple, Set, Dict, Any, List
from enum import Enum

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram.ext import filters


# =============================================================================
# Domain types
# =============================================================================

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


class Duplex(Enum):
    """Printing sides. Values are CUPS `sides` option values."""
    ONE_SIDED = "one-sided"
    TWO_SIDED_LONG = "two-sided-long-edge"
    TWO_SIDED_SHORT = "two-sided-short-edge"


class ColorMode(Enum):
    COLOR = "color"
    GRAYSCALE = "grayscale"


class PaperSize(Enum):
    """Paper size. Values are CUPS `media` option values."""
    A4 = "A4"
    LETTER = "Letter"
    LEGAL = "Legal"
    A3 = "A3"


# Ordered cycles used by the interactive UI to step through choices.
DUPLEX_CYCLE: List[Duplex] = [Duplex.ONE_SIDED, Duplex.TWO_SIDED_LONG, Duplex.TWO_SIDED_SHORT]
PAPER_CYCLE: List[PaperSize] = [PaperSize.A4, PaperSize.LETTER, PaperSize.LEGAL, PaperSize.A3]
NUP_CYCLE: List[int] = [1, 2, 4, 6]
MAX_COPIES = 99


@dataclass(frozen=True)
class PrintOptions:
    """Backend-agnostic description of how a document should be printed.

    Immutable: derive a changed copy with `dataclasses.replace`. Translation to
    a specific print backend (e.g. CUPS `lp` flags) lives in the printer
    adapter, so this stays free of backend details.
    """
    copies: int = 1
    duplex: Duplex = Duplex.ONE_SIDED
    color: ColorMode = ColorMode.COLOR
    paper_size: PaperSize = PaperSize.A4
    number_up: int = 1
    page_ranges: str = ""          # "" means all pages, else e.g. "2-5" / "1,3,5"
    printer: Optional[str] = None  # None means the system default printer
    dry_run: bool = False          # log the print command instead of executing it

    def to_dict(self) -> Dict[str, Any]:
        return {
            "copies": self.copies,
            "duplex": self.duplex.name,
            "color": self.color.name,
            "paper_size": self.paper_size.name,
            "number_up": self.number_up,
            "page_ranges": self.page_ranges,
            "printer": self.printer,
            "dry_run": self.dry_run,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PrintOptions":
        """Reconstruct from persisted dict, tolerating missing/invalid fields."""
        def _enum(enum_cls, name, default):
            try:
                return enum_cls[name]
            except (KeyError, TypeError):
                return default

        try:
            copies = max(1, min(MAX_COPIES, int(data.get("copies", 1))))
        except (TypeError, ValueError):
            copies = 1
        try:
            number_up = int(data.get("number_up", 1))
            if number_up not in NUP_CYCLE:
                number_up = 1
        except (TypeError, ValueError):
            number_up = 1

        return cls(
            copies=copies,
            duplex=_enum(Duplex, data.get("duplex"), Duplex.ONE_SIDED),
            color=_enum(ColorMode, data.get("color"), ColorMode.COLOR),
            paper_size=_enum(PaperSize, data.get("paper_size"), PaperSize.A4),
            number_up=number_up,
            page_ranges=str(data.get("page_ranges") or ""),
            printer=data.get("printer") or None,
            dry_run=bool(data.get("dry_run", False)),
        )


@dataclass
class PrintResult:
    success: bool
    job_id: Optional[str]
    message: str


@dataclass
class PrinterInfo:
    name: str
    is_default: bool = False
    description: str = ""


class JobPhase(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


@dataclass
class JobState:
    job_id: str
    phase: JobPhase
    raw: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.phase == JobPhase.COMPLETED


@dataclass
class UserSettings:
    """Per-user persisted preferences. Currently just default print options,
    kept as a wrapper so more preferences can be added without changing the
    storage interface."""
    default_options: PrintOptions = field(default_factory=PrintOptions)

    def to_dict(self) -> Dict[str, Any]:
        return {"default_options": self.default_options.to_dict()}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserSettings":
        return cls(default_options=PrintOptions.from_dict(data.get("default_options", {})))


# =============================================================================
# Command runner (single seam for all external process calls)
# =============================================================================

@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(ABC):
    """Abstraction over running external commands. A single seam keeps the
    adapters free of shell quoting and makes them trivially testable."""

    @abstractmethod
    def run(self, args: List[str], timeout: Optional[int] = None) -> CommandResult:
        pass


class SubprocessCommandRunner(CommandRunner):
    """Runs commands via subprocess with an argument list (no shell), avoiding
    shell-injection and capturing stdout/stderr."""

    def run(self, args: List[str], timeout: Optional[int] = None) -> CommandResult:
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return CommandResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as e:
            return CommandResult(124, e.stdout or "", f"timed out after {timeout}s")
        except FileNotFoundError as e:
            return CommandResult(127, "", str(e))
        except Exception as e:
            return CommandResult(1, "", str(e))


# =============================================================================
# Persistence (small JSON-backed key/value store)
# =============================================================================

class StateStore(ABC):
    """A tiny persistent dict. Implementations decide where bytes live."""

    @abstractmethod
    def load(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    def save(self, data: Dict[str, Any]) -> None:
        pass


class InMemoryStateStore(StateStore):
    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self._data: Dict[str, Any] = dict(data or {})

    def load(self) -> Dict[str, Any]:
        return dict(self._data)

    def save(self, data: Dict[str, Any]) -> None:
        self._data = dict(data)


class JsonFileStore(StateStore):
    """JSON file store with atomic writes. Missing/corrupt file reads as {}."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: Dict[str, Any]) -> None:
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.path)


# =============================================================================
# Interfaces
# =============================================================================

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
    def print_file(self, file_path: str, options: PrintOptions) -> PrintResult:
        pass

    @abstractmethod
    def list_printers(self) -> List[PrinterInfo]:
        pass

    @abstractmethod
    def get_job_state(self, job_id: str) -> JobState:
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

    @abstractmethod
    def render_preview(self, file_path: str, output_dir: str) -> Optional[str]:
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


class UserSettingsStoreInterface(ABC):
    """Abstract interface for per-user settings persistence"""

    @abstractmethod
    def get(self, user_id: int) -> UserSettings:
        pass

    @abstractmethod
    def set(self, user_id: int, settings: UserSettings) -> None:
        pass


# =============================================================================
# Adapters
# =============================================================================

class SystemPrinter(PrinterInterface):
    """System printer implementation using CUPS commands (lp/lpstat/lpq)."""

    def __init__(self, runner: Optional[CommandRunner] = None,
                 logger: Optional[logging.Logger] = None):
        self.runner = runner or SubprocessCommandRunner()
        self.logger = logger or logging.getLogger("printerbot.printer")

    def get_status(self) -> PrinterStatus:
        status = self.runner.run(["lpstat", "-p"])
        queue = self.runner.run(["lpq"])
        if not status.ok and not queue.ok:
            return PrinterStatus(status="", queue="", error=(status.stderr or queue.stderr).strip())
        return PrinterStatus(status=status.stdout.strip(), queue=queue.stdout.strip())

    def get_pending_jobs(self) -> JobStatus:
        result = self.runner.run(["lpstat", "-W", "not-completed"])
        if not result.ok:
            return JobStatus(jobs="", error=result.stderr.strip() or "lpstat failed")
        return JobStatus(jobs=result.stdout.strip())

    def get_completed_jobs(self) -> JobStatus:
        result = self.runner.run(["lpstat", "-W", "completed"])
        if not result.ok:
            return JobStatus(jobs="", error=result.stderr.strip() or "lpstat failed")
        lines = result.stdout.strip().splitlines()[:10]
        return JobStatus(jobs="\n".join(lines))

    def cancel_job(self, job_id: str) -> bool:
        return self.runner.run(["cancel", job_id]).ok

    def print_file(self, file_path: str, options: PrintOptions) -> PrintResult:
        args = ["lp"] + self._options_to_args(options) + [file_path]
        if options.dry_run:
            command = " ".join(shlex.quote(a) for a in args)
            self.logger.info("[DRY RUN] would run: %s", command)
            return PrintResult(True, None, "Dry run — command logged, nothing printed")
        result = self.runner.run(args)
        if result.ok:
            return PrintResult(True, self._parse_job_id(result.stdout), "Sent to printer")
        return PrintResult(False, None, result.stderr.strip() or "Failed to send to printer")

    def list_printers(self) -> List[PrinterInfo]:
        names_result = self.runner.run(["lpstat", "-e"])
        if not names_result.ok:
            return []
        default = self._parse_default_printer(self.runner.run(["lpstat", "-d"]).stdout)
        return [
            PrinterInfo(name=name, is_default=(name == default))
            for name in names_result.stdout.split()
        ]

    def get_job_state(self, job_id: str) -> JobState:
        if self._job_listed("not-completed", job_id):
            return JobState(job_id, JobPhase.PROCESSING)
        if self._job_listed("completed", job_id):
            return JobState(job_id, JobPhase.COMPLETED)
        return JobState(job_id, JobPhase.UNKNOWN)

    # -- internals ----------------------------------------------------------

    def _options_to_args(self, options: PrintOptions) -> List[str]:
        args: List[str] = []
        if options.printer:
            args += ["-d", options.printer]
        if options.copies and options.copies > 1:
            args += ["-n", str(options.copies)]

        o_opts = [f"sides={options.duplex.value}", f"media={options.paper_size.value}"]
        if options.color == ColorMode.GRAYSCALE:
            o_opts.append("ColorModel=Gray")
        if options.number_up and options.number_up > 1:
            o_opts.append(f"number-up={options.number_up}")
        if options.page_ranges.strip():
            o_opts.append(f"page-ranges={options.page_ranges.strip()}")

        for opt in o_opts:
            args += ["-o", opt]
        return args

    @staticmethod
    def _parse_job_id(stdout: str) -> Optional[str]:
        # lp prints e.g. "request id is Office-42 (1 file(s))"
        match = re.search(r"request id is (\S+)", stdout)
        return match.group(1) if match else None

    @staticmethod
    def _parse_default_printer(stdout: str) -> Optional[str]:
        # "system default destination: Office" or "no system default destination"
        match = re.search(r"system default destination:\s*(\S+)", stdout)
        return match.group(1) if match else None

    def _job_listed(self, which: str, job_id: str) -> bool:
        result = self.runner.run(["lpstat", "-W", which, "-o"])
        if not result.ok:
            return False
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and parts[0] == job_id:
                return True
        return False


class LibreOfficeFileProcessor(FileProcessorInterface):
    """File processor using LibreOffice / poppler utilities."""

    def __init__(self, timeout: int = 60, runner: Optional[CommandRunner] = None):
        self.timeout = timeout
        self.runner = runner or SubprocessCommandRunner()

    def convert_to_pdf(self, file_path: str, output_dir: str) -> Tuple[str, bool]:
        if self.is_pdf(file_path):
            return file_path, True

        abs_file_path = os.path.abspath(file_path)
        abs_output_dir = os.path.abspath(output_dir)

        result = self.runner.run(
            ["libreoffice", "--headless", "--convert-to", "pdf",
             abs_file_path, "--outdir", abs_output_dir],
            timeout=self.timeout,
        )
        if result.ok:
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            new_path = os.path.join(output_dir, base_name + ".pdf")
            if os.path.exists(new_path):
                return new_path, True
        return file_path, False

    def get_page_count(self, file_path: str) -> int:
        result = self.runner.run(["pdfinfo", file_path])
        if not result.ok:
            return 0
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                digits = "".join(filter(str.isdigit, line))
                return int(digits) if digits else 0
        return 0

    def is_pdf(self, file_path: str) -> bool:
        _, ext = os.path.splitext(file_path)
        return ext.lower() == ".pdf"

    def render_preview(self, file_path: str, output_dir: str) -> Optional[str]:
        """Render the first page to a PNG thumbnail. Returns the path or None."""
        out_base = os.path.join(output_dir, os.path.splitext(os.path.basename(file_path))[0] + "_preview")
        result = self.runner.run(
            ["pdftoppm", "-png", "-f", "1", "-l", "1", "-singlefile",
             "-scale-to", "1000", file_path, out_base],
            timeout=self.timeout,
        )
        out_path = out_base + ".png"
        if result.ok and os.path.exists(out_path):
            return out_path
        return None


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
    """File-based authentication manager (password from file, users in memory)."""

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


class PersistentAuthManager(AuthManagerInterface):
    """Authentication manager that persists the authorized-user set in a
    StateStore so authorizations survive restarts."""

    STORE_KEY = "authorized_users"

    def __init__(self, correct_password: str, store: StateStore):
        self.correct_password = correct_password
        self.store = store

    def _load_users(self) -> Set[int]:
        return set(self.store.load().get(self.STORE_KEY, []))

    def is_authorized(self, user_id: int) -> bool:
        return user_id in self._load_users()

    def authorize_user(self, user_id: int, password: str) -> bool:
        if password != self.correct_password:
            return False
        data = self.store.load()
        users = set(data.get(self.STORE_KEY, []))
        users.add(user_id)
        data[self.STORE_KEY] = sorted(users)
        self.store.save(data)
        return True

    def get_correct_password(self) -> str:
        return self.correct_password


class StoreBackedUserSettings(UserSettingsStoreInterface):
    """Per-user settings persisted in a StateStore (keyed by user id)."""

    STORE_KEY = "user_settings"

    def __init__(self, store: StateStore):
        self.store = store

    def get(self, user_id: int) -> UserSettings:
        bucket = self.store.load().get(self.STORE_KEY, {})
        raw = bucket.get(str(user_id))
        return UserSettings.from_dict(raw) if raw else UserSettings()

    def set(self, user_id: int, settings: UserSettings) -> None:
        data = self.store.load()
        bucket = data.setdefault(self.STORE_KEY, {})
        bucket[str(user_id)] = settings.to_dict()
        self.store.save(data)


# =============================================================================
# Service layer (business logic)
# =============================================================================

class PrinterBotService:
    """Core business logic for the printer bot"""

    def __init__(
        self,
        printer: PrinterInterface,
        file_processor: FileProcessorInterface,
        auth_manager: AuthManagerInterface,
        files_dir: str,
        settings_store: Optional[UserSettingsStoreInterface] = None,
        file_size_limit: int = 64 * 1024 * 1024,
        max_pages_limit: int = 100
    ):
        self.printer = printer
        self.file_processor = file_processor
        self.auth_manager = auth_manager
        self.files_dir = files_dir
        self.settings_store = settings_store or StoreBackedUserSettings(InMemoryStateStore())
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

    # -- per-user settings --------------------------------------------------

    def get_user_settings(self, user_id: int) -> UserSettings:
        return self.settings_store.get(user_id)

    def update_user_settings(self, user_id: int, settings: UserSettings) -> None:
        self.settings_store.set(user_id, settings)

    def default_options_for(self, user_id: int) -> PrintOptions:
        return self.get_user_settings(user_id).default_options

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


# =============================================================================
# UI building blocks (pure functions — no Telegram I/O, unit-testable)
# =============================================================================

# Scopes embedded in callback data so one set of widgets drives both a
# per-job panel ("j") and the persistent settings panel ("s").
SCOPE_JOB = "j"
SCOPE_SETTINGS = "s"

# Bot commands shown in Telegram's "/" menu — single source of truth.
BOT_COMMANDS: List[Tuple[str, str]] = [
    ("start", "Show printer status & help"),
    ("settings", "Edit your default print settings"),
    ("pending", "Show pending print jobs"),
    ("completed", "Show recently completed jobs"),
    ("cancel", "Cancel a job: /cancel <job_id>"),
]


def fenced_block(text: str) -> str:
    """Wrap (untrusted) command output in a Markdown code fence, neutralizing
    backticks so the output can't break out of the fence and trip Telegram's
    Markdown parser."""
    return "```\n" + text.replace("`", "ʼ") + "\n```"


def _next_in_cycle(value, cycle):
    try:
        idx = cycle.index(value)
    except ValueError:
        return cycle[0]
    return cycle[(idx + 1) % len(cycle)]


def apply_option_action(options: PrintOptions, verb: str,
                        printer_names: Optional[List[str]] = None) -> PrintOptions:
    """Pure transition: given current options and a UI verb, return new options."""
    if verb == "copies_inc":
        return replace(options, copies=min(MAX_COPIES, options.copies + 1))
    if verb == "copies_dec":
        return replace(options, copies=max(1, options.copies - 1))
    if verb == "duplex":
        return replace(options, duplex=_next_in_cycle(options.duplex, DUPLEX_CYCLE))
    if verb == "color":
        flipped = ColorMode.GRAYSCALE if options.color == ColorMode.COLOR else ColorMode.COLOR
        return replace(options, color=flipped)
    if verb == "paper":
        return replace(options, paper_size=_next_in_cycle(options.paper_size, PAPER_CYCLE))
    if verb == "nup":
        return replace(options, number_up=_next_in_cycle(options.number_up, NUP_CYCLE))
    if verb == "printer":
        names = printer_names or []
        if not names:
            return options
        current = options.printer if options.printer in names else names[0]
        return replace(options, printer=_next_in_cycle(current, names))
    if verb == "dryrun":
        return replace(options, dry_run=not options.dry_run)
    return options


def _duplex_label(duplex: Duplex) -> str:
    return {
        Duplex.ONE_SIDED: "Single-sided",
        Duplex.TWO_SIDED_LONG: "Double · long edge",
        Duplex.TWO_SIDED_SHORT: "Double · short edge",
    }[duplex]


def _color_label(color: ColorMode) -> str:
    return "🌈 Color" if color == ColorMode.COLOR else "⬛ Grayscale"


def build_options_keyboard(options: PrintOptions, scope: str, key: str,
                           printer_names: Optional[List[str]] = None) -> InlineKeyboardMarkup:
    """Pure render: PrintOptions -> inline keyboard. `scope`/`key` are encoded in
    every button's callback data as "<verb> <scope>:<key>"."""
    target = f"{scope}:{key}"
    copies_word = "copy" if options.copies == 1 else "copies"
    nup_word = "page" if options.number_up == 1 else "pages"
    page_label = "All pages" if not options.page_ranges else f"Pages: {options.page_ranges}"

    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("➖", callback_data=f"copies_dec {target}"),
            InlineKeyboardButton(f"{options.copies} {copies_word}", callback_data=f"noop {target}"),
            InlineKeyboardButton("➕", callback_data=f"copies_inc {target}"),
        ],
        [InlineKeyboardButton(f"Sides: {_duplex_label(options.duplex)}", callback_data=f"duplex {target}")],
        [InlineKeyboardButton(_color_label(options.color), callback_data=f"color {target}")],
        [InlineKeyboardButton(f"Paper: {options.paper_size.value}", callback_data=f"paper {target}")],
        [InlineKeyboardButton(f"{options.number_up} {nup_word}/sheet", callback_data=f"nup {target}")],
        [InlineKeyboardButton(f"📑 {page_label}", callback_data=f"range {target}")],
    ]

    names = printer_names or []
    if len(names) > 1:
        current = options.printer or "default"
        rows.append([InlineKeyboardButton(f"🖨 Printer: {current}", callback_data=f"printer {target}")])

    if scope == SCOPE_JOB:
        rows.append([
            InlineKeyboardButton("🖨️ Print", callback_data=f"print {target}"),
            InlineKeyboardButton("🗑️ Delete", callback_data=f"delete {target}"),
        ])
    else:
        # Dry run is a mode/preference, so it only appears in the settings panel;
        # per-file jobs still inherit and honor whatever default is set here.
        dry_state = "ON" if options.dry_run else "OFF"
        rows.append([InlineKeyboardButton(f"🧪 Dry run: {dry_state}", callback_data=f"dryrun {target}")])
        rows.append([InlineKeyboardButton("✅ Done", callback_data=f"done {target}")])

    return InlineKeyboardMarkup(rows)


# =============================================================================
# Telegram bot
# =============================================================================

class TelegramPrinterBot:
    """Telegram bot wrapper around PrinterBotService"""

    # how long live job-status polling runs: STATUS_POLLS * STATUS_INTERVAL secs
    STATUS_POLLS = 40
    STATUS_INTERVAL = 3

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

    def _printer_names(self) -> List[str]:
        return [p.name for p in self.service.list_printers()]

    # -- command handlers ---------------------------------------------------

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
            msg += f"📊 Current printer status:\n{fenced_block(status.status)}\n\n"
            msg += f"📋 Printer queue:\n{fenced_block(status.queue)}"
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
            msg = "✅ No pending jobs found" if not jobs.jobs else f"⏳ Pending jobs:\n{fenced_block(jobs.jobs)}"
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
        success, message = self.service.cancel_job(job_id)

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

        options = self.service.default_options_for(user_id)
        keyboard = build_options_keyboard(options, SCOPE_SETTINGS, "_", self._printer_names())
        await update.message.reply_text(
            "⚙️ Your default print settings (applied to every new file):",
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
            success, message, page_count, processed_path = self.service.process_file(file_path)

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
        options = self.service.default_options_for(user_id)
        printer_names = self._printer_names()

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

        preview_path = self.service.render_preview(processed_path)
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

        if scope == SCOPE_JOB:
            await self._handle_job_button(query, context, verb, key, user_id)
        else:
            await self._handle_settings_button(query, context, verb, key, user_id)

    async def _handle_cancel_button(self, query, job_id: str):
        success, message = self.service.cancel_job(job_id)
        emoji = "🚫" if success else "❌"
        await self._edit_panel(query, f"{emoji} {message}")

    async def _handle_job_button(self, query, context, verb, token, user_id):
        registry = self._job_registry(context)
        entry = registry.get(token)
        if not entry:
            await self._edit_panel(query, "⌛ This file is no longer available. Please re-send it.")
            return

        if verb == "delete":
            success, message = self.service.delete_file(entry["file_path"])
            registry.pop(token, None)
            await self._edit_panel(query, f"{'🗑️' if success else '❌'} {message}")
            return

        if verb == "print":
            result = self.service.print_file(entry["file_path"], entry["options"])
            registry.pop(token, None)
            if not result.success:
                await self._edit_panel(query, f"❌ {result.message}")
                return
            await self._start_live_status(query, context, result, entry["page_count"])
            return

        if verb == "range":
            await self._prompt_for_range(query, context, SCOPE_JOB, token)
            return

        # An option mutation: update the registry entry and re-render.
        entry["options"] = apply_option_action(entry["options"], verb, entry.get("printers"))
        await self._rerender_keyboard(query, entry["options"], SCOPE_JOB, token, entry.get("printers"))

    async def _handle_settings_button(self, query, context, verb, key, user_id):
        settings = self.service.get_user_settings(user_id)
        options = settings.default_options
        printer_names = self._printer_names()

        if verb == "done":
            await self._edit_panel(query, "✅ Settings saved. They'll apply to every new file.")
            return

        if verb == "range":
            await self._prompt_for_range(query, context, SCOPE_SETTINGS, key)
            return

        new_options = apply_option_action(options, verb, printer_names)
        self.service.update_user_settings(user_id, replace(settings, default_options=new_options))
        await self._rerender_keyboard(query, new_options, SCOPE_SETTINGS, key, printer_names)

    async def _rerender_keyboard(self, query, options, scope, key, printer_names):
        keyboard = build_options_keyboard(options, scope, key, printer_names)
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception as e:
            self.logger.error(f"Error re-rendering keyboard: {e}")

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
        user_id = self._get_user_id(update)

        if scope == SCOPE_JOB:
            entry = self._job_registry(context).get(key)
            if not entry:
                await update.message.reply_text("⌛ This file is no longer available. Please re-send it.")
                return
            entry["options"] = replace(entry["options"], page_ranges=page_ranges)
            options, printer_names = entry["options"], entry.get("printers")
        else:
            settings = self.service.get_user_settings(user_id)
            options = replace(settings.default_options, page_ranges=page_ranges)
            self.service.update_user_settings(user_id, replace(settings, default_options=options))
            printer_names = self._printer_names()

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

    async def _poll_job_status(self, context, chat_id, message_id, job_id, page_count, is_photo):
        seen_active = False
        cancel_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚫 Cancel", callback_data=f"cancel {job_id}")]]
        )
        try:
            for _ in range(self.STATUS_POLLS):
                await asyncio.sleep(self.STATUS_INTERVAL)
                state = self.service.get_job_state(job_id)

                if state.phase == JobPhase.COMPLETED:
                    await self._edit_by_id(context, chat_id, message_id, is_photo,
                                           f"✅ Printed {page_count} page(s) — job {job_id}")
                    return
                if state.phase in (JobPhase.PROCESSING, JobPhase.PENDING):
                    if not seen_active:
                        seen_active = True
                        await self._edit_by_id(context, chat_id, message_id, is_photo,
                                               f"🖨️ Printing… job {job_id}", cancel_kb)
                elif state.phase == JobPhase.UNKNOWN and seen_active:
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


# =============================================================================
# Wiring
# =============================================================================

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
    state_path = "./state.json"

    try:
        # Read configuration
        with open(token_path, "r") as f:
            token = f.read().strip()
        with open(password_path, "r") as f:
            password = f.read().strip()

        # Setup logging
        logger = setup_logging()

        # Persistent state shared by auth + user settings.
        state_store = JsonFileStore(state_path)

        # Create service components
        printer = SystemPrinter(logger=logger)
        file_processor = LibreOfficeFileProcessor()
        auth_manager = PersistentAuthManager(password, state_store)
        settings_store = StoreBackedUserSettings(state_store)

        # Create service
        service = PrinterBotService(
            printer=printer,
            file_processor=file_processor,
            auth_manager=auth_manager,
            files_dir=files_dir,
            settings_store=settings_store,
            file_size_limit=64 * 1024 * 1024,
            max_pages_limit=100
        )

        # Drop any leftover files from abandoned/interrupted jobs.
        swept = service.cleanup_stale_files()
        if swept:
            logger.info(f"Cleaned up {swept} stale file(s) from {files_dir}")

        # Create and run bot
        bot = TelegramPrinterBot(token, service, logger)
        bot.run()

    except Exception as e:
        logging.error(f"Fatal error starting bot: {e}")
        raise


if __name__ == '__main__':
    main()
