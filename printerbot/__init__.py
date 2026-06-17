"""Telegram printer bot.

This package was split out of a single module; ``__init__`` re-exports the
public API so existing imports (``from printerbot import X``) keep working.
"""

from .domain import (
    FileType, FileInfo, PrinterStatus, JobStatus,
    Duplex, ColorMode, PaperSize,
    DUPLEX_CYCLE, PAPER_CYCLE, NUP_CYCLE, MAX_COPIES,
    PrintOptions, PrintResult, PrinterInfo, JobPhase, JobState, UserSettings,
)
from .commands import CommandResult, CommandRunner, SubprocessCommandRunner
from .storage import StateStore, InMemoryStateStore, JsonFileStore
from .interfaces import (
    PrinterInterface, FileProcessorInterface, AuthManagerInterface,
    UserSettingsStoreInterface,
)
from .adapters import (
    SystemPrinter, LibreOfficeFileProcessor,
    InMemoryAuthManager, FileAuthManager, PersistentAuthManager,
    StoreBackedUserSettings,
)
from .service import PrinterBotService
from .ui import (
    fenced_block, apply_option_action, build_options_keyboard,
    BOT_COMMANDS, SCOPE_JOB, SCOPE_SETTINGS,
)
from .bot import TelegramPrinterBot
from .app import setup_logging, main

__all__ = [
    "FileType", "FileInfo", "PrinterStatus", "JobStatus",
    "Duplex", "ColorMode", "PaperSize",
    "DUPLEX_CYCLE", "PAPER_CYCLE", "NUP_CYCLE", "MAX_COPIES",
    "PrintOptions", "PrintResult", "PrinterInfo", "JobPhase", "JobState", "UserSettings",
    "CommandResult", "CommandRunner", "SubprocessCommandRunner",
    "StateStore", "InMemoryStateStore", "JsonFileStore",
    "PrinterInterface", "FileProcessorInterface", "AuthManagerInterface",
    "UserSettingsStoreInterface",
    "SystemPrinter", "LibreOfficeFileProcessor",
    "InMemoryAuthManager", "FileAuthManager", "PersistentAuthManager",
    "StoreBackedUserSettings",
    "PrinterBotService",
    "fenced_block", "apply_option_action", "build_options_keyboard",
    "BOT_COMMANDS", "SCOPE_JOB", "SCOPE_SETTINGS",
    "TelegramPrinterBot",
    "setup_logging", "main",
]
