'''
TOC text format:

    # offset: 3
    Preface 22
    Part I: Some Part 26
        Chapter 1 Introduction 26
            1.1 Some Section 27

Rules:
- Lines starting with '#' are comments. The plugin recognizes one comment
  directive: '# offset: N' (integer). Other comments are preserved on a
  round trip only if you write your own preservation logic; this parser
  drops them by design (kept simple).
- Blank lines are ignored.
- For non-blank, non-comment lines:
    * Leading whitespace determines nesting (we don't care whether it's
      tabs or spaces; we just compare against the indent unit established
      by the first indented line).
    * The last whitespace-delimited token is the page label (kept as a
      string -- it may be 'v', 'ix', '1', 'A-3', etc.).
    * Everything before that token is the title.
- Indent level is computed:
    * Level 0 = no leading whitespace.
    * For deeper levels, we measure each line's leading-whitespace width
      and divide by the smallest non-zero indent we've seen. If the
      indentation isn't a clean multiple, we round down. This mirrors
      what humans typically expect.
'''

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TocEntry:
    title: str
    page: str           # Always a string; may be numeric or a label like 'v'.
    level: int = 0      # 0 = top-level.


@dataclass
class ParsedToc:
    entries: List[TocEntry] = field(default_factory=list)
    offset: int = 0     # Global page offset applied at write time.


class TocParseError(ValueError):
    '''Raised when the TOC text can't be parsed.'''
    def __init__(self, message, line_number=None, line_text=None):
        super().__init__(message)
        self.line_number = line_number
        self.line_text = line_text


def _leading_ws_width(line):
    '''Count leading whitespace columns. Tabs count as 1 (we only use this
    relatively, comparing against the min non-zero width).'''
    n = 0
    for ch in line:
        if ch == ' ' or ch == '\t':
            n += 1
        else:
            break
    return n


def parse_toc(text):
    '''
    Parse TOC text into a ParsedToc. Raises TocParseError on malformed input.
    '''
    if text is None:
        return ParsedToc()

    lines = text.splitlines()

    # First pass: extract offset directive and collect non-blank, non-comment
    # lines along with their original line numbers.
    offset = 0
    raw = []  # list of (line_number, line)
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('#'):
            # Parse '# offset: N'
            directive = stripped[1:].strip()
            if directive.lower().startswith('offset:'):
                value = directive.split(':', 1)[1].strip()
                try:
                    offset = int(value)
                except ValueError:
                    raise TocParseError(
                        f"Invalid offset value: {value!r}. "
                        f"Expected an integer.",
                        line_number=i, line_text=line)
            # Other comments are dropped silently.
            continue
        raw.append((i, line))

    if not raw:
        return ParsedToc(entries=[], offset=offset)

    # Second pass: find the smallest non-zero indent width to use as the
    # indent unit.
    widths = [_leading_ws_width(line) for _, line in raw]
    nonzero = [w for w in widths if w > 0]
    indent_unit = min(nonzero) if nonzero else 1

    entries = []
    for (line_number, line), width in zip(raw, widths):
        content = line.strip()
        # Page label = last whitespace-delimited token. Title = rest.
        parts = content.rsplit(None, 1)
        if len(parts) < 2:
            raise TocParseError(
                f"Line has no page number: {content!r}. "
                f"Each entry needs a title followed by a page label.",
                line_number=line_number, line_text=line)
        title, page = parts[0].strip(), parts[1].strip()
        if not title:
            raise TocParseError(
                f"Line has no title: {content!r}.",
                line_number=line_number, line_text=line)
        level = width // indent_unit if width > 0 else 0
        entries.append(TocEntry(title=title, page=page, level=level))

    return ParsedToc(entries=entries, offset=offset)


def serialize_toc(toc, indent_unit='    '):
    '''
    Serialize a ParsedToc back to text, with the offset header if non-zero.
    '''
    lines = []
    if toc.offset != 0:
        lines.append(f'# offset: {toc.offset}')
    for entry in toc.entries:
        prefix = indent_unit * max(0, int(entry.level))
        lines.append(f'{prefix}{entry.title} {entry.page}')
    # Trailing newline so editors are happy.
    return '\n'.join(lines) + ('\n' if lines else '')


def all_pages_numeric(toc):
    '''True iff every entry's page is a positive integer string. Used to
    decide whether the global offset can be applied.'''
    if not toc.entries:
        return False
    for e in toc.entries:
        try:
            n = int(e.page)
        except ValueError:
            return False
        if n < 1:
            return False
    return True


def effective_page(entry, offset, apply_offset):
    '''
    Return the page label string to look up in /PageLabels for a given entry.

    If apply_offset is True and the entry's page is numeric, returns
    str(int(page) + offset). Otherwise returns the original page string.
    '''
    if apply_offset:
        try:
            return str(int(entry.page) + offset)
        except ValueError:
            pass
    return entry.page
