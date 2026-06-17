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


def apply_option_action(options: PrintOptions, verb: str) -> PrintOptions:
    """Pure transition for the simple, non-list controls (the copies stepper and
    the dry-run toggle). List-valued fields go through field_choices /
    apply_field_choice instead."""
    if verb == "copies_inc":
        return replace(options, copies=min(MAX_COPIES, options.copies + 1))
    if verb == "copies_dec":
        return replace(options, copies=max(1, options.copies - 1))
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


def _nup_label(n: int) -> str:
    return f"{n} page/sheet" if n == 1 else f"{n} pages/sheet"


def _printer_label(printer: Optional[str]) -> str:
    return printer if printer else "System default"


# Fields rendered as expandable sub-menus ("dropdowns"). Titles shown atop the
# sub-menu; field_choices/apply_field_choice define each field's options.
FIELD_TITLES = {
    "duplex": "Sides",
    "color": "Color",
    "paper": "Paper size",
    "nup": "Pages per sheet",
    "printer": "Printer",
}


def field_choices(field: str, options: PrintOptions,
                  printer_names: Optional[List[str]] = None) -> List[Tuple[str, bool]]:
    """Return the ordered [(label, is_selected)] choices for a selectable field.
    The list index is what the UI sends back to pick a value."""
    if field == "duplex":
        return [(_duplex_label(d), d == options.duplex) for d in DUPLEX_CYCLE]
    if field == "color":
        return [(_color_label(c), c == options.color) for c in (ColorMode.COLOR, ColorMode.GRAYSCALE)]
    if field == "paper":
        return [(p.value, p == options.paper_size) for p in PAPER_CYCLE]
    if field == "nup":
        return [(_nup_label(n), n == options.number_up) for n in NUP_CYCLE]
    if field == "printer":
        # Index 0 is always "System default" (printer=None); fixes the default
        # printer becoming unreachable once another printer was selected.
        choices = [("System default", options.printer is None)]
        choices += [(name, options.printer == name) for name in (printer_names or [])]
        return choices
    return []


def apply_field_choice(options: PrintOptions, field: str, index: int,
                       printer_names: Optional[List[str]] = None) -> PrintOptions:
    """Return new options with `field` set to its `index`-th choice. Out-of-range
    indices leave options unchanged."""
    if field == "duplex" and 0 <= index < len(DUPLEX_CYCLE):
        return replace(options, duplex=DUPLEX_CYCLE[index])
    if field == "color" and index in (0, 1):
        return replace(options, color=(ColorMode.COLOR, ColorMode.GRAYSCALE)[index])
    if field == "paper" and 0 <= index < len(PAPER_CYCLE):
        return replace(options, paper_size=PAPER_CYCLE[index])
    if field == "nup" and 0 <= index < len(NUP_CYCLE):
        return replace(options, number_up=NUP_CYCLE[index])
    if field == "printer":
        names = printer_names or []
        if index == 0:
            return replace(options, printer=None)
        if 1 <= index <= len(names):
            return replace(options, printer=names[index - 1])
    return options


def build_submenu_keyboard(field: str, options: PrintOptions, scope: str, key: str,
                           printer_names: Optional[List[str]] = None) -> InlineKeyboardMarkup:
    """Pure render: the expanded list of choices for one field. The selected
    choice is marked; each row sends "set:<field>:<index> <scope>:<key>"."""
    target = f"{scope}:{key}"
    title = FIELD_TITLES.get(field, field)
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(f"· {title} ·", callback_data=f"noop {target}")]
    ]
    for idx, (label, selected) in enumerate(field_choices(field, options, printer_names)):
        mark = "🔘 " if selected else "▫️ "
        rows.append([InlineKeyboardButton(mark + label, callback_data=f"set:{field}:{idx} {target}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"back {target}")])
    return InlineKeyboardMarkup(rows)


def build_options_keyboard(options: PrintOptions, scope: str, key: str,
                           printer_names: Optional[List[str]] = None) -> InlineKeyboardMarkup:
    """Pure render: PrintOptions -> inline keyboard. `scope`/`key` are encoded in
    every button's callback data as "<verb> <scope>:<key>"."""
    target = f"{scope}:{key}"
    copies_word = "copy" if options.copies == 1 else "copies"
    page_label = "All pages" if not options.page_ranges else f"Pages: {options.page_ranges}"

    # Selector rows ("▾") open an expandable sub-menu; copies keeps its stepper.
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("➖", callback_data=f"copies_dec {target}"),
            InlineKeyboardButton(f"{options.copies} {copies_word}", callback_data=f"noop {target}"),
            InlineKeyboardButton("➕", callback_data=f"copies_inc {target}"),
        ],
        [InlineKeyboardButton(f"Sides: {_duplex_label(options.duplex)} ▾", callback_data=f"open:duplex {target}")],
        [InlineKeyboardButton(f"Color: {_color_label(options.color)} ▾", callback_data=f"open:color {target}")],
        [InlineKeyboardButton(f"Paper: {options.paper_size.value} ▾", callback_data=f"open:paper {target}")],
        [InlineKeyboardButton(f"{_nup_label(options.number_up)} ▾", callback_data=f"open:nup {target}")],
        [InlineKeyboardButton(f"📑 {page_label}", callback_data=f"range {target}")],
    ]

    names = printer_names or []
    if len(names) > 1:
        rows.append([InlineKeyboardButton(
            f"🖨 Printer: {_printer_label(options.printer)} ▾", callback_data=f"open:printer {target}")])

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


