'''
PDF bookmark writer. Uses /PageLabels (via pypdf.PdfReader.page_labels) to
look up logical page indices by label. This avoids the toolbar-vs-internal
page-number confusion that plagues PDF bookmark tools.

Returns nothing on success; raises on failure.
'''

import logging
import os
import tempfile

# pypdf is vendored inside this plugin's ZIP at calibre_plugins/toc_bookmarker/pypdf
# When running outside Calibre (tests), fall back to the system pypdf.
try:
    from calibre_plugins.toc_bookmarker.pypdf import PdfReader, PdfWriter
except ImportError:
    from pypdf import PdfReader, PdfWriter

from calibre_plugins.toc_bookmarker.bookmarker.common import effective_page


class _CorruptCounter(logging.Handler):
    '''Counts "Ignoring wrong pointing object" log records from pypdf.'''
    def __init__(self):
        super().__init__()
        self.count = 0

    def emit(self, record):
        if 'wrong pointing object' in record.getMessage().lower():
            self.count += 1


def check_pdf_health(pdf_path):
    '''
    Open the PDF and return a dict:
      has_toc:       bool – PDF already has an outline
      corrupt_count: int  – number of corrupt cross-reference warnings
    '''
    counter = _CorruptCounter()
    pypdf_log = logging.getLogger('pypdf._reader')
    pypdf_log.addHandler(counter)
    try:
        reader = PdfReader(pdf_path)
        has_toc = bool(reader.outline)
    finally:
        pypdf_log.removeHandler(counter)
    return {'has_toc': has_toc, 'corrupt_count': counter.count}


class BookmarkError(Exception):
    '''Raised on any failure during bookmark writing or page-label lookup.'''


class MissingPageLabel(BookmarkError):
    '''The resolved page label was not found in the PDF's /PageLabels.'''
    def __init__(self, title, label):
        super().__init__(
            f"Page label {label!r} (entry: {title!r}) not found in PDF.")
        self.title = title
        self.label = label


def validate_labels(pdf_path, toc, apply_offset):
    '''
    Check that every entry's effective label exists in the PDF.

    On the first miss, raises MissingPageLabel(title, label). On success,
    returns the list of (entry, resolved_index) tuples in the same order as
    toc.entries, so the writer doesn't have to look them up again.
    '''
    reader = PdfReader(pdf_path)
    labels = reader.page_labels
    resolved = []
    for entry in toc.entries:
        label = effective_page(entry, toc.offset, apply_offset)
        try:
            idx = labels.index(str(label))
        except ValueError:
            raise MissingPageLabel(entry.title, label)
        resolved.append((entry, idx))
    return resolved


def write_bookmarks(pdf_path, toc, apply_offset):
    '''
    Write the TOC as bookmarks (outlines) into pdf_path, in place.

    Validates first (raises MissingPageLabel on miss). Atomic: writes to a
    temp file in the same directory, then os.replace().
    '''
    resolved = validate_labels(pdf_path, toc, apply_offset)

    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    # Stack-based parent tracking: parents[level] is the most recent
    # outline_item at that nesting level.
    parents = {}
    for entry, page_index in resolved:
        level = max(0, int(entry.level))
        parent = parents.get(level - 1) if level > 0 else None
        item = writer.add_outline_item(entry.title, page_index, parent=parent)
        parents[level] = item
        # Invalidate deeper levels: a new entry at level L resets all L+1, L+2...
        for deeper in [k for k in parents if k > level]:
            del parents[deeper]

    # Atomic write via temp file in same directory.
    directory = os.path.dirname(os.path.abspath(pdf_path))
    fd, tmp_path = tempfile.mkstemp(suffix='.pdf', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            writer.write(f)
        os.replace(tmp_path, pdf_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
