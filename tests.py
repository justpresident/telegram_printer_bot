#!/usr/bin/python3

import pytest
import os
import tempfile
import shutil
from unittest.mock import Mock, AsyncMock, patch, MagicMock, mock_open, call
from pathlib import Path
from dataclasses import asdict

from printerbot import (
    FileType, FileInfo, PrinterStatus, JobStatus,
    SystemPrinter, LibreOfficeFileProcessor, InMemoryAuthManager, FileAuthManager,
    PrinterBotService, TelegramPrinterBot, setup_logging
)


class TestSystemPrinter:
    def setup_method(self):
        self.printer = SystemPrinter()

    @patch('os.popen')
    def test_get_status_success(self, mock_popen):
        mock_status = Mock()
        mock_status.read.return_value = "printer ready\n"
        mock_queue = Mock()
        mock_queue.read.return_value = "no entries\n"

        mock_popen.side_effect = [mock_status, mock_queue]

        status = self.printer.get_status()

        assert status.status == "printer ready"
        assert status.queue == "no entries"
        assert status.error is None
        assert mock_popen.call_count == 2

    @patch('os.popen')
    def test_get_status_exception(self, mock_popen):
        mock_popen.side_effect = Exception("Command failed")

        status = self.printer.get_status()

        assert status.status == ""
        assert status.queue == ""
        assert status.error == "Command failed"

    @patch('os.popen')
    def test_get_pending_jobs(self, mock_popen):
        mock_result = Mock()
        mock_result.read.return_value = "job-123 user 1024 bytes\n"
        mock_popen.return_value = mock_result

        jobs = self.printer.get_pending_jobs()

        assert jobs.jobs == "job-123 user 1024 bytes"
        assert jobs.error is None
        mock_popen.assert_called_once_with('lpstat -W not-completed')

    @patch('os.popen')
    def test_get_completed_jobs(self, mock_popen):
        mock_result = Mock()
        mock_result.read.return_value = "job-456 completed\n"
        mock_popen.return_value = mock_result

        jobs = self.printer.get_completed_jobs()

        assert jobs.jobs == "job-456 completed"
        assert jobs.error is None
        mock_popen.assert_called_once_with('lpstat -W completed | head')

    @patch('os.system')
    def test_cancel_job_success(self, mock_system):
        mock_system.return_value = 0

        result = self.printer.cancel_job("job-123")

        assert result is True
        mock_system.assert_called_once_with("cancel job-123")

    @patch('os.system')
    def test_cancel_job_failure(self, mock_system):
        mock_system.return_value = 1

        result = self.printer.cancel_job("job-123")

        assert result is False

    @patch('os.system')
    def test_print_file_success(self, mock_system):
        mock_system.return_value = 0

        result = self.printer.print_file("/path/to/file.pdf")

        assert result is True
        mock_system.assert_called_once_with('lpr "/path/to/file.pdf"')

    @patch('os.system')
    def test_print_file_failure(self, mock_system):
        mock_system.return_value = 1

        result = self.printer.print_file("/path/to/file.pdf")

        assert result is False


class TestLibreOfficeFileProcessor:
    def setup_method(self):
        self.processor = LibreOfficeFileProcessor(timeout=30)

    def test_is_pdf(self):
        assert self.processor.is_pdf("test.pdf") is True
        assert self.processor.is_pdf("test.PDF") is True
        assert self.processor.is_pdf("test.docx") is False
        assert self.processor.is_pdf("test") is False

    def test_convert_to_pdf_already_pdf(self):
        with patch.object(self.processor, 'is_pdf', return_value=True):
            result_path, success = self.processor.convert_to_pdf("/path/to/file.pdf", "/output")

            assert result_path == "/path/to/file.pdf"
            assert success is True

    @patch('os.system')
    @patch('os.path.exists')
    @patch('os.path.abspath')
    def test_convert_to_pdf_success(self, mock_abspath, mock_exists, mock_system):
        mock_abspath.side_effect = lambda x: f"/abs{x}"
        mock_system.return_value = 0
        mock_exists.return_value = True

        with patch.object(self.processor, 'is_pdf', return_value=False):
            result_path, success = self.processor.convert_to_pdf("/path/to/file.docx", "/output")

            assert success is True
            assert result_path == "/output/file.pdf"
            mock_system.assert_called_once()

    @patch('os.system')
    def test_convert_to_pdf_failure(self, mock_system):
        mock_system.return_value = 1

        with patch.object(self.processor, 'is_pdf', return_value=False):
            result_path, success = self.processor.convert_to_pdf("/path/to/file.docx", "/output")

            assert success is False
            assert result_path == "/path/to/file.docx"

    @patch('os.popen')
    def test_get_page_count_success(self, mock_popen):
        mock_result = Mock()
        mock_result.read.return_value = "Pages:          5\n"
        mock_popen.return_value = mock_result

        count = self.processor.get_page_count("/path/to/file.pdf")

        assert count == 5
        mock_popen.assert_called_once_with('pdfinfo "/path/to/file.pdf" | grep Pages')

    @patch('os.popen')
    def test_get_page_count_no_pages(self, mock_popen):
        mock_result = Mock()
        mock_result.read.return_value = ""
        mock_popen.return_value = mock_result

        count = self.processor.get_page_count("/path/to/file.pdf")

        assert count == 0

    @patch('os.popen')
    def test_get_page_count_exception(self, mock_popen):
        mock_popen.side_effect = Exception("Command failed")

        count = self.processor.get_page_count("/path/to/file.pdf")

        assert count == 0


class TestInMemoryAuthManager:
    def setup_method(self):
        self.auth_manager = InMemoryAuthManager("secret123")

    def test_initial_state(self):
        assert self.auth_manager.get_correct_password() == "secret123"
        assert not self.auth_manager.is_authorized(12345)

    def test_authorize_user_correct_password(self):
        result = self.auth_manager.authorize_user(12345, "secret123")

        assert result is True
        assert self.auth_manager.is_authorized(12345)

    def test_authorize_user_wrong_password(self):
        result = self.auth_manager.authorize_user(12345, "wrong")

        assert result is False
        assert not self.auth_manager.is_authorized(12345)

    def test_multiple_users(self):
        self.auth_manager.authorize_user(111, "secret123")
        self.auth_manager.authorize_user(222, "secret123")

        assert self.auth_manager.is_authorized(111)
        assert self.auth_manager.is_authorized(222)
        assert not self.auth_manager.is_authorized(333)


class TestFileAuthManager:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.password_file = os.path.join(self.temp_dir, "password.txt")

        with open(self.password_file, 'w') as f:
            f.write("file_secret\n")

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_load_password_from_file(self):
        auth_manager = FileAuthManager(self.password_file)

        assert auth_manager.get_correct_password() == "file_secret"

    def test_load_password_file_not_found(self):
        with pytest.raises(ValueError, match="Cannot read password file"):
            FileAuthManager("/nonexistent/password.txt")

    def test_authorize_user_with_file_password(self):
        auth_manager = FileAuthManager(self.password_file)

        result = auth_manager.authorize_user(12345, "file_secret")
        assert result is True
        assert auth_manager.is_authorized(12345)

        result = auth_manager.authorize_user(67890, "wrong")
        assert result is False
        assert not auth_manager.is_authorized(67890)


class TestPrinterBotService:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_printer = Mock()
        self.mock_file_processor = Mock()
        self.mock_auth_manager = Mock()

        self.service = PrinterBotService(
            printer=self.mock_printer,
            file_processor=self.mock_file_processor,
            auth_manager=self.mock_auth_manager,
            files_dir=self.temp_dir,
            file_size_limit=1024,
            max_pages_limit=10
        )

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_get_printer_status(self):
        expected_status = PrinterStatus("ready", "empty")
        self.mock_printer.get_status.return_value = expected_status

        result = self.service.get_printer_status()

        assert result == expected_status
        self.mock_printer.get_status.assert_called_once()

    def test_authenticate_user_success(self):
        self.mock_auth_manager.is_authorized.return_value = False
        self.mock_auth_manager.authorize_user.return_value = True

        success, message = self.service.authenticate_user(12345, "password")

        assert success is True
        assert "Authorization successful" in message

    def test_authenticate_user_already_authorized(self):
        self.mock_auth_manager.is_authorized.return_value = True

        success, message = self.service.authenticate_user(12345, "password")

        assert success is False
        assert "already authorized" in message

    def test_authenticate_user_wrong_password(self):
        self.mock_auth_manager.is_authorized.return_value = False
        self.mock_auth_manager.authorize_user.return_value = False

        success, message = self.service.authenticate_user(12345, "wrong")

        assert success is False
        assert "Wrong password" in message

    def test_validate_file_too_large(self):
        file_info = FileInfo("id", 2048, "test.pdf", FileType.DOCUMENT)

        valid, message = self.service.validate_file(file_info)

        assert valid is False
        assert "too large" in message

    def test_validate_file_unknown_type(self):
        file_info = FileInfo("id", 512, "test.unknown", FileType.UNKNOWN)

        valid, message = self.service.validate_file(file_info)

        assert valid is False
        assert "Unsupported file type" in message

    def test_validate_file_success(self):
        file_info = FileInfo("id", 512, "test.pdf", FileType.DOCUMENT)

        valid, message = self.service.validate_file(file_info)

        assert valid is True
        assert message == "File is valid"

    def test_process_file_success(self):
        test_file = os.path.join(self.temp_dir, "test.pdf")
        with open(test_file, 'w') as f:
            f.write("test content")

        self.mock_file_processor.convert_to_pdf.return_value = (test_file, True)
        self.mock_file_processor.get_page_count.return_value = 5

        success, message, page_count, processed_path = self.service.process_file(test_file)

        assert success is True
        assert page_count == 5
        assert processed_path == test_file

    def test_process_file_too_many_pages(self):
        test_file = os.path.join(self.temp_dir, "test.pdf")
        with open(test_file, 'w') as f:
            f.write("test content")

        self.mock_file_processor.convert_to_pdf.return_value = (test_file, True)
        self.mock_file_processor.get_page_count.return_value = 15  # Exceeds limit of 10

        success, message, page_count, processed_path = self.service.process_file(test_file)

        assert success is False
        assert "Too many pages" in message
        assert page_count == 0

    def test_process_file_conversion_failed(self):
        test_file = os.path.join(self.temp_dir, "test.docx")
        with open(test_file, 'w') as f:
            f.write("test content")

        self.mock_file_processor.convert_to_pdf.return_value = (test_file, False)

        success, message, page_count, processed_path = self.service.process_file(test_file)

        assert success is False
        assert "Failed to convert file" in message

    def test_print_file_success(self):
        test_file = os.path.join(self.temp_dir, "test.pdf")
        with open(test_file, 'w') as f:
            f.write("test content")

        self.mock_file_processor.get_page_count.return_value = 3
        self.mock_printer.print_file.return_value = True

        success, message = self.service.print_file(test_file)

        assert success is True
        assert "3 pages" in message

    def test_print_file_not_found(self):
        success, message = self.service.print_file("/nonexistent/file.pdf")

        assert success is False
        assert "File not found" in message

    def test_delete_file_success(self):
        test_file = os.path.join(self.temp_dir, "test.pdf")
        with open(test_file, 'w') as f:
            f.write("test content")

        success, message = self.service.delete_file(test_file)

        assert success is True
        assert "File deleted" in message
        assert not os.path.exists(test_file)

    def test_cancel_job_success(self):
        self.mock_printer.cancel_job.return_value = True

        success, message = self.service.cancel_job("job-123")

        assert success is True
        assert "cancelled successfully" in message

    def test_cancel_job_invalid_id(self):
        success, message = self.service.cancel_job("invalid job id!")

        assert success is False
        assert "Invalid job_id" in message

    def test_generate_temp_filename(self):
        filename = self.service.generate_temp_filename("test.pdf")

        assert filename.endswith(".pdf")
        assert len(filename) > 4  # Should have temp name + extension

    def test_is_valid_job_id(self):
        assert self.service._is_valid_job_id("job-123") is True
        assert self.service._is_valid_job_id("job_456") is True
        assert self.service._is_valid_job_id("JobABC") is True
        assert self.service._is_valid_job_id("job 123") is False
        assert self.service._is_valid_job_id("job@123") is False


class TestTelegramPrinterBot:
    def setup_method(self):
        self.mock_service = Mock()
        self.mock_logger = Mock()
        self.bot = TelegramPrinterBot("fake_token", self.mock_service, self.mock_logger)

    def test_create_application(self):
        app = self.bot.create_application()

        assert app is not None
        assert self.bot.application == app

    def test_get_user_id_from_message(self):
        mock_update = Mock()
        mock_update.message.from_user.id = 12345
        mock_update.callback_query = None

        user_id = self.bot._get_user_id(mock_update)

        assert user_id == 12345

    def test_get_user_id_from_callback(self):
        mock_update = Mock()
        mock_update.message = None
        mock_update.callback_query.from_user.id = 67890

        user_id = self.bot._get_user_id(mock_update)

        assert user_id == 67890

    def test_get_username_with_username(self):
        mock_update = Mock()
        mock_update.message.from_user.username = "testuser"
        mock_update.message.from_user.id = 12345
        mock_update.callback_query = None

        username = self.bot._get_username(mock_update)

        assert username == "testuser"

    def test_get_username_without_username(self):
        mock_update = Mock()
        mock_update.message.from_user.username = None
        mock_update.message.from_user.id = 12345
        mock_update.callback_query = None

        username = self.bot._get_username(mock_update)

        assert username == "user_12345"

    @pytest.mark.asyncio
    async def test_start_authorized_user(self):
        mock_update = AsyncMock()
        mock_context = Mock()
        mock_update.message.from_user.id = 12345
        mock_update.message.from_user.username = "testuser"
        mock_update.callback_query = None

        self.mock_service.is_user_authorized.return_value = True
        self.mock_service.get_printer_status.return_value = PrinterStatus("ready", "empty")

        await self.bot.start(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once()
        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "authorized to print" in call_args

    @pytest.mark.asyncio
    async def test_start_unauthorized_user(self):
        mock_update = AsyncMock()
        mock_context = Mock()
        mock_update.message.from_user.id = 12345
        mock_update.message.from_user.username = "testuser"
        mock_update.callback_query = None

        self.mock_service.is_user_authorized.return_value = False

        await self.bot.start(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once_with(
            "Please authorize by \"/auth <password>\"."
        )

    @pytest.mark.asyncio
    async def test_authorize_success(self):
        mock_update = AsyncMock()
        mock_context = Mock()
        mock_context.args = ["secret123"]
        mock_update.message.from_user.id = 12345
        mock_update.message.from_user.username = "testuser"
        mock_update.callback_query = None

        self.mock_service.authenticate_user.return_value = (True, "Authorization successful!")

        await self.bot.authorize(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once()
        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "🎉" in call_args

    @pytest.mark.asyncio
    async def test_authorize_no_password(self):
        mock_update = AsyncMock()
        mock_context = Mock()
        mock_context.args = []

        await self.bot.authorize(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once()
        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "Please provide password" in call_args

    def test_extract_file_info_document(self):
        mock_message = Mock()
        mock_message.document.file_id = "doc123"
        mock_message.document.file_size = 1024
        mock_message.document.file_name = "test.pdf"
        mock_message.photo = None

        file_info = self.bot._extract_file_info(mock_message)

        assert file_info.file_id == "doc123"
        assert file_info.file_size == 1024
        assert file_info.file_name == "test.pdf"
        assert file_info.file_type == FileType.DOCUMENT

    def test_extract_file_info_photo(self):
        mock_message = Mock()
        mock_message.document = None
        mock_photo = Mock()
        mock_photo.file_id = "photo123"
        mock_photo.file_size = 2048
        mock_photo.file_unique_id = "unique123"
        mock_message.photo = [mock_photo]

        file_info = self.bot._extract_file_info(mock_message)

        assert file_info.file_id == "photo123"
        assert file_info.file_size == 2048
        assert file_info.file_name == "photo_unique123.jpg"
        assert file_info.file_type == FileType.PHOTO

    def test_extract_file_info_none(self):
        mock_message = Mock()
        mock_message.document = None
        mock_message.photo = None

        file_info = self.bot._extract_file_info(mock_message)

        assert file_info is None


class TestSetupLogging:
    def test_setup_logging(self):
        logger = setup_logging()

        assert logger.level == 20  # INFO level
        assert len(logger.handlers) >= 2  # File and console handlers

    @patch('logging.FileHandler')
    @patch('logging.StreamHandler')
    def test_setup_logging_handlers(self, mock_stream_handler, mock_file_handler):
        mock_file_handler.return_value = Mock()
        mock_stream_handler.return_value = Mock()

        logger = setup_logging()

        mock_file_handler.assert_called_once_with("printerbot.log")
        mock_stream_handler.assert_called_once()


# Integration tests
class TestIntegration:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.password_file = os.path.join(self.temp_dir, "password.txt")

        with open(self.password_file, 'w') as f:
            f.write("integration_test_password")

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    @patch('os.system')
    @patch('os.popen')
    def test_full_integration(self, mock_popen, mock_system):
        # Setup mocks
        mock_status = Mock()
        mock_status.read.return_value = "printer ready"
        mock_queue = Mock()
        mock_queue.read.return_value = "no entries"
        mock_popen.side_effect = [mock_status, mock_queue]
        mock_system.return_value = 0

        # Create components
        printer = SystemPrinter()
        file_processor = LibreOfficeFileProcessor()
        auth_manager = FileAuthManager(self.password_file)

        service = PrinterBotService(
            printer=printer,
            file_processor=file_processor,
            auth_manager=auth_manager,
            files_dir=self.temp_dir
        )

        # Test authentication
        success, message = service.authenticate_user(12345, "integration_test_password")
        assert success is True

        # Test authorization check
        assert service.is_user_authorized(12345) is True

        # Test printer status
        status = service.get_printer_status()
        assert status.status == "printer ready"
        assert status.queue == "no entries"


if __name__ == '__main__':
    # Run with: python -m pytest test_printer_bot.py -v
    pytest.main([__file__, '-v'])
