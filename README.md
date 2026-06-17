# Telegram printer bot

![CI](https://github.com/justpresident/telegram_printer_bot/actions/workflows/ci.yml/badge.svg)
![Coverage](./.github/badges/coverage.svg)

This is a simple bot that allows you to print on your home printer from anywhere from any device that has a telegram client.
It also exposes some basic printer diagnostics and print job management.
In order to print something, just upload a file without any commands. A lot of formats are supported.

When you send a file, the bot replies with a **first-page preview** and an interactive
**print-options panel**. Tap the buttons to adjust how it prints, then press *Print*.
After printing, the message keeps you posted on the job's progress and offers a *Cancel* button.

## Commands:
 * `/start` - Starts interaction and prints current state
 * `/auth <password>` - Authenticates the user with a password against the password stored in the `auth_password` file
 * `/settings` - Opens a panel to edit your **default** print options (applied to every new file)
 * `/pending` - Lists all pending print jobs
 * `/completed` - Lists last 10 completed print jobs
 * `/cancel <job_id>` - Cancels a pending job with a given id

The full command list is also registered with Telegram, so it shows up in the `/` autocomplete menu.

## Print options
Both the per-file panel and `/settings` expose the same controls (translated to CUPS `lp` options):

 * **Copies** — `➖ / ➕` stepper
 * **Sides** — single-sided / double-sided (long or short edge)
 * **Color** — color / grayscale
 * **Paper size** — A4 / Letter / Legal / A3
 * **Pages per sheet** — 1 / 2 / 4 / 6 (N-up)
 * **Page range** — type e.g. `2-5` or `1,3,5`, or `all`
 * **Printer** — when more than one CUPS printer is available, pick the target

Per-file choices start from your saved `/settings` defaults and can be tweaked just for that file.

### Dry run
`/settings` also has a **🧪 Dry run** toggle. When it's on, sending a file (or pressing *Print*)
logs the exact `lp` command that *would* run, to `printerbot.log`, and reports back that nothing
was printed — useful for testing the bot without wasting paper. It's a per-user preference, so it
applies to every file you print until you turn it off.

## Details
 * Password authentication; authorized users are **persisted** across restarts (in `state.json`)
 * Per-user default print settings are persisted in the same `state.json`
 * Uses `libreoffice` to convert files to PDF format before printing
 * Uses `pdftoppm` (poppler) to render the first-page preview
 * Uses `lp` (CUPS) to send a file to the printer with the chosen options, and `lpstat`/`cancel` for status and cancellation
 * Uses the `python-telegram-bot` package to interface with the Telegram API

## Architecture
The code is a small `printerbot/` package, split into single-responsibility modules, each
collaborator behind an interface so it can be swapped or tested in isolation:

| Module | Responsibility |
| --- | --- |
| `domain.py` | Plain data types — `PrintOptions`, `PrintResult`, `JobState`, `UserSettings`, enums |
| `commands.py` | `CommandRunner` — the single seam for external commands (no shell, captures output) |
| `storage.py` | `StateStore` — persistent key/value store with atomic `update()` (`JsonFileStore` / `InMemoryStateStore`) |
| `interfaces.py` | Abstract `PrinterInterface`, `FileProcessorInterface`, `AuthManagerInterface`, `UserSettingsStoreInterface` |
| `adapters.py` | `SystemPrinter` (CUPS), `LibreOfficeFileProcessor`, auth + settings stores |
| `service.py` | `PrinterBotService` — backend-agnostic business logic |
| `ui.py` | Pure keyboard/option logic (`build_options_keyboard`, `apply_option_action`) — unit-tested without Telegram |
| `bot.py` | `TelegramPrinterBot` — Telegram I/O; offloads blocking work off the event loop |
| `app.py` | `setup_logging` + `main` wiring |

`printerbot/__init__.py` re-exports the public API, so `from printerbot import …` works from anywhere.

# Install Dependencies

* Python 3.11+ and the `python-telegram-bot` package: `pip3 install python-telegram-bot`
* LibreOffice and poppler-utils (`pdftoppm`, `pdfinfo`). Install with your package manager,
  e.g. `apt install libreoffice poppler-utils`
* CUPS, with at least one configured printer (`lp`, `lpstat`, `cancel`)

# Running the bot
Put your bot token in `./token` and the shared password in `./auth_password`, then run from the
project directory:
```
python3 -m printerbot
```
A `printerbot.service` systemd unit is included for running it as a service.

# Running the tests
```
pip3 install pytest pytest-asyncio
python3 -m pytest tests.py -v
```

To measure coverage (as CI does):
```
pip3 install pytest-cov
python3 -m pytest tests.py --cov=printerbot --cov-report=term-missing
```
