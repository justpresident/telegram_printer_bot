"""Concrete adapters: CUPS printer, LibreOffice/poppler files, auth & settings."""

import os
import re
import shlex
import logging
from typing import Optional, Tuple, Set, List

from .domain import (
    PrinterStatus, JobStatus, PrintOptions, PrintResult, PrinterInfo,
    JobState, JobPhase, ColorMode, UserSettings,
)
from .commands import CommandRunner, SubprocessCommandRunner
from .storage import StateStore
from .interfaces import (
    PrinterInterface, FileProcessorInterface, AuthManagerInterface,
    UserSettingsStoreInterface,
)


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
        # lpstat lists completed jobs oldest-first; the last 10 are the newest.
        lines = result.stdout.strip().splitlines()[-10:]
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

        def mutate(data):
            users = set(data.get(self.STORE_KEY, []))
            users.add(user_id)
            data[self.STORE_KEY] = sorted(users)

        self.store.update(mutate)
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
        def mutate(data):
            bucket = data.setdefault(self.STORE_KEY, {})
            bucket[str(user_id)] = settings.to_dict()

        self.store.update(mutate)


