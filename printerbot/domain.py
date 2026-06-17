"""Domain types: file/print descriptions, options, results, job state."""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum


class FileType(Enum):
    DOCUMENT = "document"
    PHOTO = "photo"
    UNKNOWN = "unknown"


@dataclass
class FileInfo:
    file_id: str
    file_size: int
    file_name: str
    file_type: FileType


@dataclass
class PrinterStatus:
    status: str
    queue: str
    error: Optional[str] = None


@dataclass
class JobStatus:
    jobs: str
    error: Optional[str] = None


class Duplex(Enum):
    """Printing sides. Values are CUPS `sides` option values."""
    ONE_SIDED = "one-sided"
    TWO_SIDED_LONG = "two-sided-long-edge"
    TWO_SIDED_SHORT = "two-sided-short-edge"


class ColorMode(Enum):
    COLOR = "color"
    GRAYSCALE = "grayscale"


class PaperSize(Enum):
    """Paper size. Values are CUPS `media` option values."""
    A4 = "A4"
    LETTER = "Letter"
    LEGAL = "Legal"
    A3 = "A3"


# Ordered cycles used by the interactive UI to step through choices.
DUPLEX_CYCLE: List[Duplex] = [Duplex.ONE_SIDED, Duplex.TWO_SIDED_LONG, Duplex.TWO_SIDED_SHORT]
PAPER_CYCLE: List[PaperSize] = [PaperSize.A4, PaperSize.LETTER, PaperSize.LEGAL, PaperSize.A3]
NUP_CYCLE: List[int] = [1, 2, 4, 6]
MAX_COPIES = 99


@dataclass(frozen=True)
class PrintOptions:
    """Backend-agnostic description of how a document should be printed.

    Immutable: derive a changed copy with `dataclasses.replace`. Translation to
    a specific print backend (e.g. CUPS `lp` flags) lives in the printer
    adapter, so this stays free of backend details.
    """
    copies: int = 1
    duplex: Duplex = Duplex.ONE_SIDED
    color: ColorMode = ColorMode.COLOR
    paper_size: PaperSize = PaperSize.A4
    number_up: int = 1
    page_ranges: str = ""          # "" means all pages, else e.g. "2-5" / "1,3,5"
    printer: Optional[str] = None  # None means the system default printer
    dry_run: bool = False          # log the print command instead of executing it

    def to_dict(self) -> Dict[str, Any]:
        return {
            "copies": self.copies,
            "duplex": self.duplex.name,
            "color": self.color.name,
            "paper_size": self.paper_size.name,
            "number_up": self.number_up,
            "page_ranges": self.page_ranges,
            "printer": self.printer,
            "dry_run": self.dry_run,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PrintOptions":
        """Reconstruct from persisted dict, tolerating missing/invalid fields."""
        def _enum(enum_cls, name, default):
            try:
                return enum_cls[name]
            except (KeyError, TypeError):
                return default

        try:
            copies = max(1, min(MAX_COPIES, int(data.get("copies", 1))))
        except (TypeError, ValueError):
            copies = 1
        try:
            number_up = int(data.get("number_up", 1))
            if number_up not in NUP_CYCLE:
                number_up = 1
        except (TypeError, ValueError):
            number_up = 1

        return cls(
            copies=copies,
            duplex=_enum(Duplex, data.get("duplex"), Duplex.ONE_SIDED),
            color=_enum(ColorMode, data.get("color"), ColorMode.COLOR),
            paper_size=_enum(PaperSize, data.get("paper_size"), PaperSize.A4),
            number_up=number_up,
            page_ranges=str(data.get("page_ranges") or ""),
            printer=data.get("printer") or None,
            dry_run=bool(data.get("dry_run", False)),
        )


@dataclass
class PrintResult:
    success: bool
    job_id: Optional[str]
    message: str


@dataclass
class PrinterInfo:
    name: str
    is_default: bool = False
    description: str = ""


class JobPhase(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


@dataclass
class JobState:
    job_id: str
    phase: JobPhase
    raw: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.phase == JobPhase.COMPLETED


@dataclass
class UserSettings:
    """Per-user persisted preferences. Currently just default print options,
    kept as a wrapper so more preferences can be added without changing the
    storage interface."""
    default_options: PrintOptions = field(default_factory=PrintOptions)

    def to_dict(self) -> Dict[str, Any]:
        return {"default_options": self.default_options.to_dict()}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserSettings":
        return cls(default_options=PrintOptions.from_dict(data.get("default_options", {})))


