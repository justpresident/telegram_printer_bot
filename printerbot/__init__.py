"""Telegram printer bot.

This package was split out of a single module; ``__init__`` re-exports the
public API so existing imports (``from printerbot import X``) keep working.
"""

from .domain import (
    FileType, FileInfo, PrinterStatus, JobStatus,
    Duplex, ColorMode, PaperSize,
    DUPLEX_CYCLE, PAPER_CYCLE, NUP_CYCLE, MAX_COPIES,
    PrintOptions, PrintResult, PrinterInfo, JobPhase, JobState, printer_key,
)
from .commands import CommandResult, CommandRunner, SubprocessCommandRunner
from .storage import StateStore, InMemoryStateStore, JsonFileStore
from .interfaces import (
    PrinterInterface, FileProcessorInterface, AuthManagerInterface,
    PrinterSettingsStoreInterface,
)
from .adapters import (
    SystemPrinter, LibreOfficeFileProcessor,
    InMemoryAuthManager, FileAuthManager, PersistentAuthManager,
    StoreBackedPrinterSettings,
)
from .service import PrinterBotService
from .ui import (
    fenced_block, apply_option_action, apply_field_choice, field_choices,
    build_options_keyboard, build_submenu_keyboard,
    BOT_COMMANDS, SCOPE_JOB, SCOPE_SETTINGS,
)
from .bot import TelegramPrinterBot
from .app import setup_logging, main

__all__ = [
    "FileType", "FileInfo", "PrinterStatus", "JobStatus",
    "Duplex", "ColorMode", "PaperSize",
    "DUPLEX_CYCLE", "PAPER_CYCLE", "NUP_CYCLE", "MAX_COPIES",
    "PrintOptions", "PrintResult", "PrinterInfo", "JobPhase", "JobState", "printer_key",
    "CommandResult", "CommandRunner", "SubprocessCommandRunner",
    "StateStore", "InMemoryStateStore", "JsonFileStore",
    "PrinterInterface", "FileProcessorInterface", "AuthManagerInterface",
    "PrinterSettingsStoreInterface",
    "SystemPrinter", "LibreOfficeFileProcessor",
    "InMemoryAuthManager", "FileAuthManager", "PersistentAuthManager",
    "StoreBackedPrinterSettings",
    "PrinterBotService",
    "fenced_block", "apply_option_action", "apply_field_choice", "field_choices",
    "build_options_keyboard", "build_submenu_keyboard",
    "BOT_COMMANDS", "SCOPE_JOB", "SCOPE_SETTINGS",
    "TelegramPrinterBot",
    "setup_logging", "main",
]
