"""Pure UI helpers: inline-keyboard builders and option transitions (no Telegram I/O)."""

from typing import Optional, Tuple, List
from dataclasses import replace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .domain import (
    PrintOptions, Duplex, ColorMode,
    DUPLEX_CYCLE, PAPER_CYCLE, NUP_CYCLE, MAX_COPIES,
)


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


