#!/usr/bin/python3

import pytest
import os
import tempfile
import shutil
from unittest.mock import Mock, AsyncMock, patch
from dataclasses import replace

from printerbot import (
    FileType, FileInfo, PrinterStatus, JobStatus,
    SystemPrinter, LibreOfficeFileProcessor, InMemoryAuthManager, FileAuthManager,
    PrinterBotService, TelegramPrinterBot, setup_logging,
    # new domain + infra
    PrintOptions, PrintResult, PrinterInfo, JobState, JobPhase, UserSettings,
    Duplex, ColorMode, PaperSize,
    CommandRunner, CommandResult,
    InMemoryStateStore, JsonFileStore,
    PersistentAuthManager, StoreBackedUserSettings,
    # UI pure functions
    apply_option_action, build_options_keyboard, SCOPE_JOB, SCOPE_SETTINGS,
)


class FakeCommandRunner(CommandRunner):
    """Test double for CommandRunner. Either drive it with a handler callable
    (args -> CommandResult) or queue ordered responses; falls back to default."""

    def __init__(self, handler=None, default=None):
        self.calls = []
        self.handler = handler
        self.responses = []
        self.default = default or CommandResult(0, "", "")

    def run(self, args, timeout=None):
        self.calls.append(list(args))
        if self.handler:
            return self.handler(list(args))
        if self.responses:
            return self.responses.pop(0)
        return self.default


# =============================================================================
# SystemPrinter (CUPS adapter)
# =============================================================================

class TestSystemPrinter:
    def _printer(self, handler):
        return SystemPrinter(runner=FakeCommandRunner(handler=handler))

    def test_get_status_success(self):
        def handler(args):
            if args == ["lpstat", "-p"]:
                return CommandResult(0, "printer ready\n", "")
            if args == ["lpq"]:
                return CommandResult(0, "no entries\n", "")
            return CommandResult(1, "", "unexpected")

        status = self._printer(handler).get_status()
        assert status.status == "printer ready"
        assert status.queue == "no entries"
        assert status.error is None

    def test_get_status_error(self):
        printer = SystemPrinter(runner=FakeCommandRunner(default=CommandResult(1, "", "boom")))
        status = printer.get_status()
        assert status.error == "boom"

    def test_get_pending_jobs(self):
        runner = FakeCommandRunner(default=CommandResult(0, "job-123 user 1024 bytes\n", ""))
        jobs = SystemPrinter(runner=runner).get_pending_jobs()
        assert jobs.jobs == "job-123 user 1024 bytes"
        assert jobs.error is None
        assert runner.calls[0] == ["lpstat", "-W", "not-completed"]

    def test_get_completed_jobs_truncates_to_ten(self):
        lines = "\n".join(f"job-{i}" for i in range(20))
        runner = FakeCommandRunner(default=CommandResult(0, lines, ""))
        jobs = SystemPrinter(runner=runner).get_completed_jobs()
        assert jobs.jobs.count("\n") == 9  # 10 lines
        assert runner.calls[0] == ["lpstat", "-W", "completed"]

    def test_cancel_job_success(self):
        runner = FakeCommandRunner(default=CommandResult(0, "", ""))
        assert SystemPrinter(runner=runner).cancel_job("job-123") is True
        assert runner.calls[0] == ["cancel", "job-123"]

    def test_cancel_job_failure(self):
        printer = SystemPrinter(runner=FakeCommandRunner(default=CommandResult(1, "", "no")))
        assert printer.cancel_job("job-123") is False

    def test_print_file_success_parses_job_id(self):
        runner = FakeCommandRunner(default=CommandResult(0, "request id is Office-7 (1 file(s))", ""))
        result = SystemPrinter(runner=runner).print_file("/x/file.pdf", PrintOptions())
        assert result.success is True
        assert result.job_id == "Office-7"
        # default single copy: no -n; file is last arg
        assert runner.calls[0][0] == "lp"
        assert runner.calls[0][-1] == "/x/file.pdf"

    def test_print_file_failure(self):
        runner = FakeCommandRunner(default=CommandResult(1, "", "printer offline"))
        result = SystemPrinter(runner=runner).print_file("/x/file.pdf", PrintOptions())
        assert result.success is False
        assert result.job_id is None
        assert "printer offline" in result.message

    def test_options_translate_to_lp_args(self):
        runner = FakeCommandRunner(default=CommandResult(0, "request id is P-1 (1 file(s))", ""))
        options = PrintOptions(
            copies=3, duplex=Duplex.TWO_SIDED_LONG, color=ColorMode.GRAYSCALE,
            paper_size=PaperSize.LETTER, number_up=2, page_ranges="2-5", printer="Office",
        )
        SystemPrinter(runner=runner).print_file("/x/file.pdf", options)
        args = runner.calls[0]
        assert args[:1] == ["lp"]
        assert "-d" in args and args[args.index("-d") + 1] == "Office"
        assert "-n" in args and args[args.index("-n") + 1] == "3"
        joined = " ".join(args)
        assert "sides=two-sided-long-edge" in joined
        assert "media=Letter" in joined
        assert "ColorModel=Gray" in joined
        assert "number-up=2" in joined
        assert "page-ranges=2-5" in joined

    def test_list_printers(self):
        def handler(args):
            if args == ["lpstat", "-e"]:
                return CommandResult(0, "Office\nBasement\n", "")
            if args == ["lpstat", "-d"]:
                return CommandResult(0, "system default destination: Basement", "")
            return CommandResult(1, "", "")

        printers = self._printer(handler).list_printers()
        assert [p.name for p in printers] == ["Office", "Basement"]
        assert printers[1].is_default is True
        assert printers[0].is_default is False

    def test_get_job_state_phases(self):
        def make(which_active):
            def handler(args):
                # args like ["lpstat", "-W", "<which>", "-o"]
                which = args[2]
                if which == which_active:
                    return CommandResult(0, "Office-7 user 1024 ...\n", "")
                return CommandResult(0, "", "")
            return handler

        assert self._printer(make("not-completed")).get_job_state("Office-7").phase == JobPhase.PROCESSING
        assert self._printer(make("completed")).get_job_state("Office-7").phase == JobPhase.COMPLETED
        assert self._printer(make("none")).get_job_state("Office-7").phase == JobPhase.UNKNOWN


# =============================================================================
# LibreOfficeFileProcessor
# =============================================================================

class TestLibreOfficeFileProcessor:
    def test_is_pdf(self):
        p = LibreOfficeFileProcessor()
        assert p.is_pdf("test.pdf") is True
        assert p.is_pdf("test.PDF") is True
        assert p.is_pdf("test.docx") is False
        assert p.is_pdf("test") is False

    def test_convert_to_pdf_already_pdf(self):
        p = LibreOfficeFileProcessor()
        result_path, success = p.convert_to_pdf("/path/to/file.pdf", "/output")
        assert result_path == "/path/to/file.pdf"
        assert success is True

    def test_convert_to_pdf_success(self):
        runner = FakeCommandRunner(default=CommandResult(0, "", ""))
        p = LibreOfficeFileProcessor(runner=runner)
        with patch("os.path.exists", return_value=True):
            result_path, success = p.convert_to_pdf("/path/to/file.docx", "/output")
        assert success is True
        assert result_path == "/output/file.pdf"
        assert runner.calls[0][0] == "libreoffice"

    def test_convert_to_pdf_failure(self):
        runner = FakeCommandRunner(default=CommandResult(1, "", "convert failed"))
        p = LibreOfficeFileProcessor(runner=runner)
        result_path, success = p.convert_to_pdf("/path/to/file.docx", "/output")
        assert success is False
        assert result_path == "/path/to/file.docx"

    def test_get_page_count_success(self):
        runner = FakeCommandRunner(default=CommandResult(0, "Title: x\nPages:          5\n", ""))
        assert LibreOfficeFileProcessor(runner=runner).get_page_count("/f.pdf") == 5

    def test_get_page_count_no_pages(self):
        runner = FakeCommandRunner(default=CommandResult(0, "Title: x\n", ""))
        assert LibreOfficeFileProcessor(runner=runner).get_page_count("/f.pdf") == 0

    def test_get_page_count_command_error(self):
        runner = FakeCommandRunner(default=CommandResult(1, "", "boom"))
        assert LibreOfficeFileProcessor(runner=runner).get_page_count("/f.pdf") == 0

    def test_render_preview_success(self):
        runner = FakeCommandRunner(default=CommandResult(0, "", ""))
        p = LibreOfficeFileProcessor(runner=runner)
        with patch("os.path.exists", return_value=True):
            out = p.render_preview("/dir/file.pdf", "/dir")
        assert out == "/dir/file_preview.png"
        assert runner.calls[0][0] == "pdftoppm"

    def test_render_preview_failure(self):
        runner = FakeCommandRunner(default=CommandResult(1, "", "no poppler"))
        p = LibreOfficeFileProcessor(runner=runner)
        assert p.render_preview("/dir/file.pdf", "/dir") is None


# =============================================================================
# PrintOptions domain model
# =============================================================================

class TestPrintOptions:
    def test_defaults(self):
        o = PrintOptions()
        assert o.copies == 1
        assert o.duplex == Duplex.ONE_SIDED
        assert o.color == ColorMode.COLOR
        assert o.paper_size == PaperSize.A4
        assert o.number_up == 1
        assert o.page_ranges == ""
        assert o.printer is None

    def test_roundtrip_serialization(self):
        o = PrintOptions(copies=4, duplex=Duplex.TWO_SIDED_SHORT, color=ColorMode.GRAYSCALE,
                         paper_size=PaperSize.A3, number_up=4, page_ranges="1,3", printer="Office")
        assert PrintOptions.from_dict(o.to_dict()) == o

    def test_from_dict_tolerates_garbage(self):
        o = PrintOptions.from_dict({"copies": "nope", "duplex": "BOGUS", "number_up": 999})
        assert o.copies == 1
        assert o.duplex == Duplex.ONE_SIDED
        assert o.number_up == 1

    def test_from_dict_clamps_copies(self):
        assert PrintOptions.from_dict({"copies": 9999}).copies == 99
        assert PrintOptions.from_dict({"copies": 0}).copies == 1


# =============================================================================
# UI pure functions
# =============================================================================

class TestApplyOptionAction:
    def test_copies_bounds(self):
        assert apply_option_action(PrintOptions(copies=1), "copies_dec").copies == 1
        assert apply_option_action(PrintOptions(copies=1), "copies_inc").copies == 2
        assert apply_option_action(PrintOptions(copies=99), "copies_inc").copies == 99

    def test_duplex_cycles(self):
        o = apply_option_action(PrintOptions(), "duplex")
        assert o.duplex == Duplex.TWO_SIDED_LONG
        o = apply_option_action(o, "duplex")
        assert o.duplex == Duplex.TWO_SIDED_SHORT
        o = apply_option_action(o, "duplex")
        assert o.duplex == Duplex.ONE_SIDED

    def test_color_toggles(self):
        o = apply_option_action(PrintOptions(), "color")
        assert o.color == ColorMode.GRAYSCALE
        assert apply_option_action(o, "color").color == ColorMode.COLOR

    def test_paper_and_nup_cycle(self):
        assert apply_option_action(PrintOptions(), "paper").paper_size == PaperSize.LETTER
        assert apply_option_action(PrintOptions(), "nup").number_up == 2

    def test_printer_cycles_through_list(self):
        names = ["A", "B", "C"]
        o = apply_option_action(PrintOptions(printer=None), "printer", names)
        assert o.printer == "B"  # current resolves to A, next is B
        assert apply_option_action(PrintOptions(printer="C"), "printer", names).printer == "A"

    def test_printer_noop_without_printers(self):
        assert apply_option_action(PrintOptions(), "printer", []).printer is None

    def test_unknown_verb_is_noop(self):
        o = PrintOptions(copies=2)
        assert apply_option_action(o, "whatever") == o


class TestBuildOptionsKeyboard:
    def _callbacks(self, markup):
        return [btn.callback_data for row in markup.inline_keyboard for btn in row]

    def test_job_scope_has_print_and_delete(self):
        markup = build_options_keyboard(PrintOptions(), SCOPE_JOB, "tok", [])
        cbs = self._callbacks(markup)
        assert "print j:tok" in cbs
        assert "delete j:tok" in cbs
        assert "done j:tok" not in cbs

    def test_settings_scope_has_done(self):
        markup = build_options_keyboard(PrintOptions(), SCOPE_SETTINGS, "_", [])
        cbs = self._callbacks(markup)
        assert "done s:_" in cbs
        assert "print s:_" not in cbs

    def test_printer_button_only_when_multiple(self):
        one = build_options_keyboard(PrintOptions(), SCOPE_JOB, "t", ["A"])
        many = build_options_keyboard(PrintOptions(), SCOPE_JOB, "t", ["A", "B"])
        assert not any("printer" in c for c in self._callbacks(one))
        assert any(c == "printer j:t" for c in self._callbacks(many))

    def test_callback_data_within_telegram_limit(self):
        markup = build_options_keyboard(PrintOptions(), SCOPE_JOB, "deadbeef", ["A", "B"])
        for c in self._callbacks(markup):
            assert len(c.encode()) <= 64


# =============================================================================
# Persistence: state stores, persistent auth, settings
# =============================================================================

class TestJsonFileStore:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.path = os.path.join(self.temp_dir, "state.json")

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_missing_file_reads_empty(self):
        assert JsonFileStore(self.path).load() == {}

    def test_save_then_load(self):
        store = JsonFileStore(self.path)
        store.save({"a": 1, "b": [1, 2]})
        assert JsonFileStore(self.path).load() == {"a": 1, "b": [1, 2]}

    def test_corrupt_file_reads_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        assert JsonFileStore(self.path).load() == {}


class TestPersistentAuthManager:
    def test_authorize_persists_across_instances(self):
        store = InMemoryStateStore()
        a1 = PersistentAuthManager("secret", store)
        assert a1.authorize_user(111, "secret") is True
        assert a1.is_authorized(111)
        # A fresh manager backed by the same store still sees the user.
        a2 = PersistentAuthManager("secret", store)
        assert a2.is_authorized(111)

    def test_wrong_password_rejected(self):
        store = InMemoryStateStore()
        a = PersistentAuthManager("secret", store)
        assert a.authorize_user(111, "nope") is False
        assert not a.is_authorized(111)


class TestStoreBackedUserSettings:
    def test_default_when_absent(self):
        s = StoreBackedUserSettings(InMemoryStateStore())
        assert s.get(42).default_options == PrintOptions()

    def test_set_then_get(self):
        store = InMemoryStateStore()
        s = StoreBackedUserSettings(store)
        opts = PrintOptions(copies=3, color=ColorMode.GRAYSCALE)
        s.set(42, UserSettings(default_options=opts))
        # New instance, same store -> persisted.
        assert StoreBackedUserSettings(store).get(42).default_options == opts

    def test_settings_isolated_per_user(self):
        s = StoreBackedUserSettings(InMemoryStateStore())
        s.set(1, UserSettings(PrintOptions(copies=5)))
        assert s.get(2).default_options.copies == 1


# =============================================================================
# Auth managers (unchanged behaviour)
# =============================================================================

class TestInMemoryAuthManager:
    def setup_method(self):
        self.auth_manager = InMemoryAuthManager("secret123")

    def test_initial_state(self):
        assert self.auth_manager.get_correct_password() == "secret123"
        assert not self.auth_manager.is_authorized(12345)

    def test_authorize_user_correct_password(self):
        assert self.auth_manager.authorize_user(12345, "secret123") is True
        assert self.auth_manager.is_authorized(12345)

    def test_authorize_user_wrong_password(self):
        assert self.auth_manager.authorize_user(12345, "wrong") is False
        assert not self.auth_manager.is_authorized(12345)


class TestFileAuthManager:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.password_file = os.path.join(self.temp_dir, "password.txt")
        with open(self.password_file, 'w') as f:
            f.write("file_secret\n")

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_load_password_from_file(self):
        assert FileAuthManager(self.password_file).get_correct_password() == "file_secret"

    def test_load_password_file_not_found(self):
        with pytest.raises(ValueError, match="Cannot read password file"):
            FileAuthManager("/nonexistent/password.txt")

    def test_authorize_user_with_file_password(self):
        auth_manager = FileAuthManager(self.password_file)
        assert auth_manager.authorize_user(12345, "file_secret") is True
        assert auth_manager.is_authorized(12345)
        assert auth_manager.authorize_user(67890, "wrong") is False


# =============================================================================
# Service layer
# =============================================================================

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

    def _make_file(self, name="test.pdf"):
        path = os.path.join(self.temp_dir, name)
        with open(path, 'w') as f:
            f.write("x")
        return path

    def test_get_printer_status(self):
        expected = PrinterStatus("ready", "empty")
        self.mock_printer.get_status.return_value = expected
        assert self.service.get_printer_status() == expected

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
        valid, message = self.service.validate_file(FileInfo("id", 2048, "t.pdf", FileType.DOCUMENT))
        assert valid is False
        assert "too large" in message

    def test_validate_file_unknown_type(self):
        valid, message = self.service.validate_file(FileInfo("id", 512, "t", FileType.UNKNOWN))
        assert valid is False
        assert "Unsupported file type" in message

    def test_validate_file_success(self):
        valid, message = self.service.validate_file(FileInfo("id", 512, "t.pdf", FileType.DOCUMENT))
        assert valid is True

    def test_process_file_success(self):
        test_file = self._make_file()
        self.mock_file_processor.convert_to_pdf.return_value = (test_file, True)
        self.mock_file_processor.get_page_count.return_value = 5
        success, message, page_count, processed_path = self.service.process_file(test_file)
        assert success is True
        assert page_count == 5
        assert processed_path == test_file

    def test_process_file_too_many_pages(self):
        test_file = self._make_file()
        self.mock_file_processor.convert_to_pdf.return_value = (test_file, True)
        self.mock_file_processor.get_page_count.return_value = 15
        success, message, page_count, _ = self.service.process_file(test_file)
        assert success is False
        assert "Too many pages" in message

    def test_process_file_conversion_failed(self):
        test_file = self._make_file("test.docx")
        self.mock_file_processor.convert_to_pdf.return_value = (test_file, False)
        success, message, _, _ = self.service.process_file(test_file)
        assert success is False
        assert "Failed to convert file" in message

    def test_print_file_success_returns_result(self):
        test_file = self._make_file()
        self.mock_file_processor.get_page_count.return_value = 3
        self.mock_printer.print_file.return_value = PrintResult(True, "Office-9", "Sent to printer")
        result = self.service.print_file(test_file, PrintOptions(copies=2))
        assert isinstance(result, PrintResult)
        assert result.success is True
        assert result.job_id == "Office-9"
        assert "3 pages" in result.message
        assert "2 copies" in result.message
        # Options are passed through to the printer adapter.
        assert self.mock_printer.print_file.call_args[0][1].copies == 2

    def test_print_file_not_found(self):
        result = self.service.print_file("/nonexistent/file.pdf")
        assert result.success is False
        assert "File not found" in result.message

    def test_print_file_printer_failure(self):
        test_file = self._make_file()
        self.mock_file_processor.get_page_count.return_value = 1
        self.mock_printer.print_file.return_value = PrintResult(False, None, "offline")
        result = self.service.print_file(test_file)
        assert result.success is False
        assert "offline" in result.message

    def test_render_preview_delegates(self):
        test_file = self._make_file()
        self.mock_file_processor.render_preview.return_value = "/tmp/x.png"
        assert self.service.render_preview(test_file) == "/tmp/x.png"

    def test_render_preview_invalid_path(self):
        assert self.service.render_preview("/nope/x.pdf") is None

    def test_delete_file_success(self):
        test_file = self._make_file()
        success, message = self.service.delete_file(test_file)
        assert success is True
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

    def test_list_printers_swallows_errors(self):
        self.mock_printer.list_printers.side_effect = RuntimeError("boom")
        assert self.service.list_printers() == []

    def test_user_settings_roundtrip(self):
        opts = PrintOptions(copies=4)
        self.service.update_user_settings(7, UserSettings(default_options=opts))
        assert self.service.default_options_for(7) == opts

    def test_generate_temp_filename(self):
        filename = self.service.generate_temp_filename("test.pdf")
        assert filename.endswith(".pdf")
        assert len(filename) > 4

    def test_is_valid_job_id(self):
        assert self.service._is_valid_job_id("job-123") is True
        assert self.service._is_valid_job_id("job_456") is True
        assert self.service._is_valid_job_id("job 123") is False
        assert self.service._is_valid_job_id("job@123") is False


# =============================================================================
# Telegram bot
# =============================================================================

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
        assert self.bot._get_user_id(mock_update) == 12345

    def test_get_user_id_from_callback(self):
        mock_update = Mock()
        mock_update.message = None
        mock_update.callback_query.from_user.id = 67890
        assert self.bot._get_user_id(mock_update) == 67890

    def test_get_username_with_username(self):
        mock_update = Mock()
        mock_update.message.from_user.username = "testuser"
        mock_update.message.from_user.id = 12345
        mock_update.callback_query = None
        assert self.bot._get_username(mock_update) == "testuser"

    def test_get_username_without_username(self):
        mock_update = Mock()
        mock_update.message.from_user.username = None
        mock_update.message.from_user.id = 12345
        mock_update.callback_query = None
        assert self.bot._get_username(mock_update) == "user_12345"

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
        assert "authorized to print" in mock_update.message.reply_text.call_args[0][0]

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
        assert "🎉" in mock_update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_authorize_no_password(self):
        mock_update = AsyncMock()
        mock_context = Mock()
        mock_context.args = []
        await self.bot.authorize(mock_update, mock_context)
        assert "Please provide password" in mock_update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_settings_command_shows_panel(self):
        mock_update = AsyncMock()
        mock_context = Mock()
        mock_update.message.from_user.id = 12345
        mock_update.message.from_user.username = "u"
        mock_update.callback_query = None
        self.mock_service.is_user_authorized.return_value = True
        self.mock_service.default_options_for.return_value = PrintOptions()
        self.mock_service.list_printers.return_value = []
        await self.bot.settings(mock_update, mock_context)
        kwargs = mock_update.message.reply_text.call_args.kwargs
        assert kwargs.get("reply_markup") is not None

    @pytest.mark.asyncio
    async def test_button_option_mutation_updates_registry(self):
        query = AsyncMock()
        query.data = "copies_inc j:tok"
        query.from_user.id = 1
        query.from_user.username = "u"
        query.message.photo = None
        mock_update = Mock()
        mock_update.message = None
        mock_update.callback_query = query
        context = Mock()
        context.user_data = {"jobs": {"tok": {
            "file_path": "/x.pdf", "page_count": 1,
            "options": PrintOptions(copies=1), "printers": [],
        }}}
        self.mock_service.is_user_authorized.return_value = True

        await self.bot.button(mock_update, context)

        assert context.user_data["jobs"]["tok"]["options"].copies == 2
        query.edit_message_reply_markup.assert_awaited()

    @pytest.mark.asyncio
    async def test_button_print_sends_to_printer(self):
        query = AsyncMock()
        query.data = "print j:tok"
        query.from_user.id = 1
        query.from_user.username = "u"
        query.message.photo = None
        mock_update = Mock()
        mock_update.message = None
        mock_update.callback_query = query
        context = Mock()
        context.user_data = {"jobs": {"tok": {
            "file_path": "/x.pdf", "page_count": 2,
            "options": PrintOptions(), "printers": [],
        }}}
        self.mock_service.is_user_authorized.return_value = True
        # job_id None -> no background polling task started
        self.mock_service.print_file.return_value = PrintResult(True, None, "Sent to printer! (2 pages)")

        await self.bot.button(mock_update, context)

        self.mock_service.print_file.assert_called_once()
        assert "tok" not in context.user_data["jobs"]
        assert "Sent to printer" in query.edit_message_text.call_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_button_unauthorized(self):
        query = AsyncMock()
        query.data = "print j:tok"
        query.from_user.id = 1
        query.from_user.username = "u"
        mock_update = Mock()
        mock_update.message = None
        mock_update.callback_query = query
        self.mock_service.is_user_authorized.return_value = False
        await self.bot.button(mock_update, Mock())
        query.answer.assert_awaited_with("❌ Not authorized")

    @pytest.mark.asyncio
    async def test_range_input_updates_job_options(self):
        mock_update = AsyncMock()
        mock_update.message.text = "2-5"
        mock_update.message.from_user.id = 1
        mock_update.message.from_user.username = "u"
        mock_update.callback_query = None
        context = Mock()
        context.bot = AsyncMock()
        context.user_data = {
            "awaiting_range": {"scope": SCOPE_JOB, "key": "tok", "chat_id": 9, "message_id": 8},
            "jobs": {"tok": {"file_path": "/x.pdf", "page_count": 5,
                             "options": PrintOptions(), "printers": []}},
        }
        self.mock_service.is_user_authorized.return_value = True
        await self.bot.text_callback(mock_update, context)
        assert context.user_data["jobs"]["tok"]["options"].page_ranges == "2-5"
        context.bot.edit_message_reply_markup.assert_awaited()

    @pytest.mark.asyncio
    async def test_range_input_rejects_garbage(self):
        mock_update = AsyncMock()
        mock_update.message.text = "not a range"
        mock_update.message.from_user.id = 1
        mock_update.message.from_user.username = "u"
        mock_update.callback_query = None
        context = Mock()
        context.user_data = {
            "awaiting_range": {"scope": SCOPE_JOB, "key": "tok", "chat_id": 9, "message_id": 8},
            "jobs": {"tok": {"file_path": "/x.pdf", "page_count": 5,
                             "options": PrintOptions(), "printers": []}},
        }
        self.mock_service.is_user_authorized.return_value = True
        await self.bot.text_callback(mock_update, context)
        # unchanged
        assert context.user_data["jobs"]["tok"]["options"].page_ranges == ""
        assert "Invalid range" in mock_update.message.reply_text.call_args[0][0]

    def test_extract_file_info_document(self):
        mock_message = Mock()
        mock_message.document.file_id = "doc123"
        mock_message.document.file_size = 1024
        mock_message.document.file_name = "test.pdf"
        mock_message.photo = None
        file_info = self.bot._extract_file_info(mock_message)
        assert file_info.file_id == "doc123"
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
        assert file_info.file_name == "photo_unique123.jpg"
        assert file_info.file_type == FileType.PHOTO

    def test_extract_file_info_none(self):
        mock_message = Mock()
        mock_message.document = None
        mock_message.photo = None
        assert self.bot._extract_file_info(mock_message) is None


class TestSetupLogging:
    def test_setup_logging(self):
        logger = setup_logging()
        assert logger.level == 20
        assert len(logger.handlers) >= 2


# =============================================================================
# Integration
# =============================================================================

class TestIntegration:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.password_file = os.path.join(self.temp_dir, "password.txt")
        with open(self.password_file, 'w') as f:
            f.write("integration_test_password")

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_full_integration(self):
        def handler(args):
            if args == ["lpstat", "-p"]:
                return CommandResult(0, "printer ready", "")
            if args == ["lpq"]:
                return CommandResult(0, "no entries", "")
            return CommandResult(0, "", "")

        printer = SystemPrinter(runner=FakeCommandRunner(handler=handler))
        file_processor = LibreOfficeFileProcessor(runner=FakeCommandRunner())
        store = InMemoryStateStore()
        auth_manager = PersistentAuthManager("integration_test_password", store)

        service = PrinterBotService(
            printer=printer,
            file_processor=file_processor,
            auth_manager=auth_manager,
            files_dir=self.temp_dir,
            settings_store=StoreBackedUserSettings(store),
        )

        assert service.authenticate_user(12345, "integration_test_password")[0] is True
        assert service.is_user_authorized(12345) is True

        status = service.get_printer_status()
        assert status.status == "printer ready"
        assert status.queue == "no entries"

        # Settings persist through the same shared store.
        service.update_user_settings(12345, UserSettings(PrintOptions(copies=2)))
        assert service.default_options_for(12345).copies == 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
