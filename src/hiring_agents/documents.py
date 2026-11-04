"""Supporting documents for a request: course outlines, job descriptions, selection guidelines.

The client adds documents to the store (candidate_data/documents/) once and picks which ones a
request draft should use. The Manager agent reads their text to decide the weights, the role
and the instructions it gives each ranker, and pulls out course session times for the
Scheduler (see drafting.py). Rankers never see the documents themselves, only what the Manager
wrote from them, so each ranker stays focused on its own criterion.

Formats: .pdf (text-based; scanned pages have no text), .docx, .txt and .md.

Before any text reaches an LLM, `scrub` replaces email addresses, phone numbers and long
numbers (student/employee IDs) with placeholders. Names (e.g. the instructor's) can't be found
reliably and are left in, so the Manager is told not to repeat people's names. Text is cut to
MAX_DOC_CHARS per document and MAX_TOTAL_CHARS per request, with a warning.
"""

import logging
import re
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .schemas import DocumentInfo

logger = logging.getLogger(__name__)

DOC_TYPES = {".pdf", ".docx", ".txt", ".md"}
MAX_FILE_BYTES = 20_000_000
MAX_PDF_PAGES = 100
MAX_DOC_CHARS = 20_000      # per document sent to the Manager (~5k tokens)
MAX_TOTAL_CHARS = 40_000    # all documents in one request

_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_SCRUB = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"), "[phone]"),
    (re.compile(r"(?<!\d)\d{7,}(?!\d)"), "[number]"),
]


class DocumentError(ValueError):
    """A document can't be used (unsupported, unreadable, or no text). The message says why."""


def _docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        raise DocumentError(f"{path.name} isn't a readable Word document.") from e
    # One line per paragraph; table cells are paragraphs too.
    return "\n".join("".join(t.text or "" for t in p.iter(f"{_WORD}t")) for p in root.iter(f"{_WORD}p"))


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError(f"{path.name} is password-protected.")
        return "\n".join(page.extract_text() or "" for page in reader.pages[:MAX_PDF_PAGES])
    except PdfReadError as e:
        raise DocumentError(f"{path.name} isn't a readable PDF: {e}") from e


def extract_text(path: Path) -> str:
    """The document's plain text, with runs of blank lines collapsed.

    Raises:
        DocumentError: Unsupported type, too large, unreadable, or no text (e.g. a scanned PDF).
    """
    kind = path.suffix.lower()
    if kind not in DOC_TYPES:
        raise DocumentError(f"Unsupported document type: {path.name}. Use {sorted(DOC_TYPES)}.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DocumentError(f"{path.name} is larger than {MAX_FILE_BYTES // 1_000_000} MB.")
    if kind == ".pdf":
        text = _pdf_text(path)
    elif kind == ".docx":
        text = _docx_text(path)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise DocumentError(f"No text found in {path.name}"
                            + (" (a scanned PDF? Save it with text, or as .docx)." if kind == ".pdf" else "."))
    return text


def scrub(text: str) -> str:
    """Replace email addresses, phone numbers and long ID-like numbers with placeholders."""
    for pattern, placeholder in _SCRUB:
        text = pattern.sub(placeholder, text)
    return text


# --- The store's documents folder ---

def find_documents(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in DOC_TYPES)


def describe(path: Path) -> DocumentInfo:
    try:
        chars, problem = len(extract_text(path)), ""
    except DocumentError as e:
        chars, problem = 0, str(e)
    return DocumentInfo(name=path.name, kind=path.suffix.lower().lstrip("."), chars=chars,
                        truncated=chars > MAX_DOC_CHARS, problem=problem)


def add_documents(paths: list[Path], folder: Path, replace: bool = False) -> tuple[list[str], list[str]]:
    """Copy documents into the store after checking each has text.

    Returns:
        (added, replaced) file names.

    Raises:
        DocumentError: For an unreadable document or a name clash without `replace`. Nothing
            is copied if any document fails.
    """
    names = [p.name for p in paths]
    if len(set(names)) < len(names):
        raise DocumentError(f"Two documents share a name: {sorted({n for n in names if names.count(n) > 1})}.")
    for p in paths:
        extract_text(p)
    existing = [n for n in names if (folder / n).exists()]
    if existing and not replace:
        raise DocumentError(f"Already in the store: {existing}. Upload with replace to overwrite them.")
    folder.mkdir(parents=True, exist_ok=True)
    for p in paths:
        shutil.copy2(p, folder / p.name)
    logger.info("Documents added: %s (replaced %s)", [n for n in names if n not in existing], existing)
    return [n for n in names if n not in existing], existing


def remove_document(name: str, folder: Path) -> None:
    path = folder / Path(name).name
    if path.suffix.lower() not in DOC_TYPES or not path.exists():
        raise DocumentError(f"No document named {name!r}.")
    path.unlink()
    logger.info("Document removed: %s", path.name)


def load_context(names: list[str], folder: Path) -> tuple[list[dict[str, str]], list[str]]:
    """The scrubbed, size-capped text of the named documents, for the Manager.

    Returns:
        ([{"name", "text"}, ...], warnings)

    Raises:
        DocumentError: For a name that isn't in the store, or a document with no text.
    """
    docs, warnings, budget = [], [], MAX_TOTAL_CHARS
    for name in dict.fromkeys(names):     # unique, in order
        path = folder / Path(name).name
        if not path.exists():
            raise DocumentError(f"No document named {name!r}. Add it first.")
        text = scrub(extract_text(path))
        limit = min(MAX_DOC_CHARS, budget)
        if len(text) > limit:
            warnings.append(f"{path.name}: only the first {limit:,} of {len(text):,} characters were read.")
            text = text[:limit]
        if text:
            docs.append({"name": path.name, "text": text})
        budget -= len(text)
    logger.info("Documents for the Manager: %s", [(d["name"], len(d["text"])) for d in docs])
    return docs, warnings
