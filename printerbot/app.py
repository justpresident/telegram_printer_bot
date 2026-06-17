"""Process entry point: logging setup and main() wiring."""

import logging

from .service import PrinterBotService
from .adapters import (
    SystemPrinter, LibreOfficeFileProcessor, PersistentAuthManager,
    StoreBackedUserSettings,
)
from .storage import JsonFileStore
from .bot import TelegramPrinterBot


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

