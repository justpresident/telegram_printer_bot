"""Abstract interfaces for the bot's collaborators (printer, files, auth, settings)."""

from abc import ABC, abstractmethod
from typing import Optional, Tuple, List

from .domain import (
    PrinterStatus, JobStatus, PrintOptions, PrintResult, PrinterInfo, JobState,
)


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


class PrinterSettingsStoreInterface(ABC):
    """Abstract interface for per-printer default-settings persistence.
    Keyed by printer name; the system-default printer uses the empty string."""

    @abstractmethod
    def get(self, printer_key: str) -> PrintOptions:
        pass

    @abstractmethod
    def set(self, printer_key: str, options: PrintOptions) -> None:
        pass


