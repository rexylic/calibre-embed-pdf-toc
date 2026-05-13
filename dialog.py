'''
Main dialog for Embed ToC.

UI flow:
    - Tabbed editor: Text tab (QPlainTextEdit) and Tree tab (QTreeWidget).
    - Both views share an in-memory ParsedToc. Switching tabs serializes the
      currently active view into ParsedToc and re-populates the other.
    - If parsing fails when leaving a tab, the switch is blocked and the
      user sees an inline error.
    - Apply button: validates page labels exist in the PDF, then writes.
    - On failure (missing label): error dialog naming the entry, return to
      editor.
    - On success: a post-write dialog with Open PDF / Undo / Close.
'''

import os
import shutil
import subprocess
import sys

from qt.core import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QIcon, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, Qt,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from calibre.gui2 import error_dialog, gprefs, info_dialog, question_dialog

from calibre_plugins.toc_bookmarker.bookmarker.common import (
    ParsedToc, TocEntry, TocParseError,
    all_pages_numeric, parse_toc, serialize_toc,
)
from calibre_plugins.toc_bookmarker.bookmarker.pdf import (
    BookmarkError, MissingPageLabel, check_pdf_health, write_bookmarks,
)
from calibre_plugins.toc_bookmarker.config import prefs


TOC_FILENAME = 'toc'
ORIGINAL_BACKUP_SUFFIX = '.original_pdf'
DIRTY_BACKUP_SUFFIX = '.dirty_pdf'

PLACEHOLDER = (
    "# Example TOC. Indentation defines nesting; the last token on each line\n"
    "# is the page label (numeric or roman, e.g. 'v', 'ix').\n"
    "#\n"
    "# A '# offset: N' line applies a global integer offset to numeric\n"
    "# entries when writing -- useful when the printed page numbers are\n"
    "# off from the PDF's internal page labels.\n"
    "#\n"
    "Preface v\n"
    "Chapter 1 Introduction 1\n"
    "    1.1 Background 3\n"
    "    1.2 Outline 8\n"
    "Chapter 2 Methods 15\n"
)


TAB_WIDTH = 4
_SOFT_TAB = ' ' * TAB_WIDTH


class TocTextEdit(QPlainTextEdit):
    '''A QPlainTextEdit with soft tabs (TAB_WIDTH spaces) and Shift+Tab
    dedent. Also fixes the tab-stop display width so tab characters in
    loaded files render at TAB_WIDTH columns instead of Qt's default 8.'''

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabChangesFocus(False)
        self._apply_tab_stop_distance()

    def setFont(self, font):
        super().setFont(font)
        self._apply_tab_stop_distance()

    def _apply_tab_stop_distance(self):
        # Pixel width of TAB_WIDTH spaces in the current font. Set this as
        # the tab stop so a literal '\t' renders at the same width as our
        # soft-tab insertions.
        metrics = self.fontMetrics()
        # horizontalAdvance is available in Qt 5.11+; safe for Calibre.
        try:
            space_w = metrics.horizontalAdvance(' ')
        except AttributeError:
            space_w = metrics.width(' ')
        self.setTabStopDistance(space_w * TAB_WIDTH)

    def keyPressEvent(self, event):
        key = event.key()

        # Shift+Tab: dedent the selection (or current line).
        if key == Qt.Key.Key_Backtab:
            self._dedent_selection()
            return

        if key == Qt.Key.Key_Tab:
            cursor = self.textCursor()
            if cursor.hasSelection():
                self._indent_selection()
                return
            # No selection: insert TAB_WIDTH spaces aligned to the next
            # tab column relative to the line start.
            block_text = cursor.block().text()
            col = cursor.positionInBlock()
            # How many spaces to reach the next multiple of TAB_WIDTH?
            spaces = TAB_WIDTH - (col % TAB_WIDTH)
            if spaces == 0:
                spaces = TAB_WIDTH
            cursor.insertText(' ' * spaces)
            return

        super().keyPressEvent(event)

    def _selected_block_range(self, cursor):
        '''Return (first_block_number, last_block_number) covering the
        cursor's selection (or just the cursor's current block if none).'''
        if not cursor.hasSelection():
            n = cursor.block().blockNumber()
            return n, n
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        doc = self.document()
        a = doc.findBlock(start).blockNumber()
        b = doc.findBlock(end).blockNumber()
        # If the selection ends exactly at the start of a block, don't
        # include that block in the range.
        if doc.findBlock(end).position() == end and b > a:
            b -= 1
        return a, b

    def _indent_selection(self):
        cursor = self.textCursor()
        first, last = self._selected_block_range(cursor)
        doc = self.document()
        cursor.beginEditBlock()
        for bn in range(first, last + 1):
            block = doc.findBlockByNumber(bn)
            c = self.textCursor()
            c.setPosition(block.position())
            c.insertText(_SOFT_TAB)
        cursor.endEditBlock()

    def _dedent_selection(self):
        cursor = self.textCursor()
        first, last = self._selected_block_range(cursor)
        doc = self.document()
        cursor.beginEditBlock()
        for bn in range(first, last + 1):
            block = doc.findBlockByNumber(bn)
            text = block.text()
            # Remove up to TAB_WIDTH leading spaces, or a single leading tab.
            to_remove = 0
            if text.startswith('\t'):
                to_remove = 1
            else:
                while to_remove < TAB_WIDTH and to_remove < len(text) and text[to_remove] == ' ':
                    to_remove += 1
            if to_remove == 0:
                continue
            c = self.textCursor()
            c.setPosition(block.position())
            c.setPosition(block.position() + to_remove,
                          c.MoveMode.KeepAnchor)
            c.removeSelectedText()
        cursor.endEditBlock()


# ---------- Tree widget ----------

class TocTree(QTreeWidget):
    '''Two columns: Title and Page. Level is implicit from tree depth.
    Supports drag-and-drop reordering and re-parenting (InternalMove).

    Warnings (⚠ icon + tooltip on the Title cell):
      - Level skip: an entry whose declared indent level in the source data
        skips a level (e.g. jumps from depth 1 to depth 3). Detected only
        on load(); cleared by any subsequent structural edit.
      - Page out of order: a numeric page that is less than the preceding
        numeric page in DFS pre-order. Recomputed live after every edit.
    '''

    COL_TITLE = 0
    COL_PAGE = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderLabels(['Title', 'Page'])
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDropIndicatorShown(True)
        self.setRootIsDecorated(True)
        header = self.header()
        header.setSectionResizeMode(self.COL_TITLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_PAGE, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(self.COL_PAGE, 80)
        self.itemChanged.connect(lambda _: self.refresh_warnings())

    # ---- Warning helpers ----

    def _warn_icon(self):
        return self.style().standardIcon(
            self.style().StandardPixmap.SP_MessageBoxWarning)

    def _append_warning(self, item, msg):
        existing = item.toolTip(self.COL_TITLE)
        new_tip = (existing + '\n' + msg) if existing else msg
        item.setIcon(self.COL_TITLE, self._warn_icon())
        item.setToolTip(self.COL_TITLE, new_tip)

    def _clear_warning(self, item):
        item.setIcon(self.COL_TITLE, QIcon())
        item.setToolTip(self.COL_TITLE, '')

    def _dfs_items(self):
        result = []
        def walk(item):
            result.append(item)
            for i in range(item.childCount()):
                walk(item.child(i))
        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        return result

    def _check_page_order(self, items):
        '''Flag items whose numeric page is less than the preceding numeric page.'''
        prev_page = None
        for item in items:
            try:
                page = int(item.text(self.COL_PAGE).strip())
            except ValueError:
                continue  # Non-numeric: skip but don't reset the reference.
            if prev_page is not None and page < prev_page:
                self._append_warning(
                    item,
                    f'Page {page} is less than the preceding numeric page {prev_page}.')
            prev_page = page

    def refresh_warnings(self):
        '''Clear all warnings and recheck page ordering. Called after any
        structural edit or in-place cell change.'''
        self.blockSignals(True)
        try:
            items = self._dfs_items()
            for item in items:
                self._clear_warning(item)
            self._check_page_order(items)
        finally:
            self.blockSignals(False)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.refresh_warnings()

    # ---- Item construction ----

    def _make_item(self, title, page):
        item = QTreeWidgetItem([title, page])
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEditable
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsDropEnabled
        )
        return item

    # ---- Load / dump ----

    def load(self, entries):
        self.blockSignals(True)
        try:
            self.clear()
            if not entries:
                return
            # Stack-based reconstruction: stack holds (declared_level, item).
            stack = []
            for entry in entries:
                item = self._make_item(entry.title, entry.page)
                while stack and stack[-1][0] >= entry.level:
                    stack.pop()
                parent_level = stack[-1][0] if stack else -1
                if stack:
                    stack[-1][1].addChild(item)
                else:
                    self.addTopLevelItem(item)
                stack.append((entry.level, item))
                # Warn if the declared level skips over one or more levels.
                if entry.level > parent_level + 1:
                    self._append_warning(
                        item,
                        f'Indentation skips a level: declared level {entry.level}, '
                        f'but the deepest available level here is {parent_level + 1}.')
            self.expandAll()
            self._check_page_order(self._dfs_items())
        finally:
            self.blockSignals(False)

    def dump_entries(self):
        '''Walk tree and return a flat list of TocEntry. Raises TocParseError
        if any item is missing a title or page.'''
        out = []
        for i in range(self.topLevelItemCount()):
            self._collect(self.topLevelItem(i), 0, out)
        return out

    def _collect(self, item, level, out):
        title = item.text(self.COL_TITLE).strip()
        page = item.text(self.COL_PAGE).strip()
        if title or page:
            if not title:
                raise TocParseError(f'An entry has a page ({page!r}) but no title.')
            if not page:
                raise TocParseError(f'Entry {title!r} has no page.')
            out.append(TocEntry(title=title, page=page, level=level))
        for i in range(item.childCount()):
            self._collect(item.child(i), level + 1, out)

    # ---- Editing operations ----

    def add_entry(self):
        '''Insert a blank sibling after the current item, or a top-level
        item if nothing is selected.'''
        current = self.currentItem()
        item = self._make_item('', '')
        if current:
            parent = current.parent()
            if parent:
                idx = parent.indexOfChild(current)
                parent.insertChild(idx + 1, item)
            else:
                idx = self.indexOfTopLevelItem(current)
                self.insertTopLevelItem(idx + 1, item)
        else:
            self.addTopLevelItem(item)
        self.setCurrentItem(item)
        self.editItem(item, self.COL_TITLE)
        self.refresh_warnings()

    def delete_selected(self):
        # Remove only items that aren't already descendants of another selected
        # item (removing a parent takes its subtree, so the child is gone too).
        selected = self.selectedItems()
        if not selected:
            return
        selected_ids = {id(it) for it in selected}

        def has_selected_ancestor(item):
            p = item.parent()
            while p:
                if id(p) in selected_ids:
                    return True
                p = p.parent()
            return False

        roots = [it for it in selected if not has_selected_ancestor(it)]
        for item in roots:
            parent = item.parent()
            if parent:
                parent.removeChild(item)
            else:
                self.takeTopLevelItem(self.indexOfTopLevelItem(item))
        self.refresh_warnings()

    def _items_in_tree_order(self, items):
        '''Return items sorted by DFS pre-order position in the tree.'''
        item_ids = {id(it) for it in items}
        result = []

        def walk(item):
            if id(item) in item_ids:
                result.append(item)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        return result

    def indent_selected(self):
        '''Make each selected item a child of its preceding sibling.'''
        items = self._items_in_tree_order(self.selectedItems())
        for item in items:
            self._indent_item(item)
        for item in items:
            item.setSelected(True)
        self.refresh_warnings()

    def _indent_item(self, item):
        parent = item.parent()
        if parent:
            idx = parent.indexOfChild(item)
            if idx == 0:
                return
            new_parent = parent.child(idx - 1)
            parent.removeChild(item)
            new_parent.addChild(item)
            new_parent.setExpanded(True)
        else:
            idx = self.indexOfTopLevelItem(item)
            if idx <= 0:
                return
            new_parent = self.topLevelItem(idx - 1)
            self.takeTopLevelItem(idx)
            new_parent.addChild(item)
            new_parent.setExpanded(True)

    def dedent_selected(self):
        '''Move each selected item one level up, placing it after its parent.
        Items already at top level are skipped.'''
        items = self._items_in_tree_order(self.selectedItems())
        for item in items:
            self._dedent_item(item)
        for item in items:
            item.setSelected(True)
        self.refresh_warnings()

    def _dedent_item(self, item):
        parent = item.parent()
        if not parent:
            return
        grandparent = parent.parent()
        if grandparent:
            idx = grandparent.indexOfChild(parent)
            parent.removeChild(item)
            grandparent.insertChild(idx + 1, item)
        else:
            idx = self.indexOfTopLevelItem(parent)
            parent.removeChild(item)
            self.insertTopLevelItem(idx + 1, item)


# ---------- PDF health-check dialog ----------

class _PdfHealthDialog(QDialog):
    '''
    Shown before writing bookmarks when issues are detected:
      - PDF already has an outline (will be overwritten).
      - PDF has corrupt cross-reference entries (pypdf warnings).

    Possible outcomes (self.action):
      'cancel'   – user cancelled; abort the apply.
      'continue' – user wants to proceed despite issues.
      'fix'      – user wants to run Ghostscript first, then apply.
    '''

    def __init__(self, parent, has_toc, corrupt_count, gs_path):
        super().__init__(parent)
        self.action = 'cancel'
        self.setWindowTitle('PDF Health Check')
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)

        # ---- Issue list ----
        if has_toc:
            toc_label = QLabel(
                '<b>⚠ This PDF already has a table of contents.</b><br>'
                'Applying will overwrite the existing outline.'
            )
            toc_label.setWordWrap(True)
            layout.addWidget(toc_label)

        if corrupt_count:
            corrupt_label = QLabel(
                f'<b>⚠ {corrupt_count} corrupt object reference(s) detected.</b><br>'
                'Some PDF readers (e.g. KOReader) may not display the TOC correctly '
                'on a file with this many internal errors. It is recommended to clean '
                'the PDF with Ghostscript before embedding a TOC.'
            )
            corrupt_label.setWordWrap(True)
            layout.addWidget(corrupt_label)

            # GS fix section
            gs_note = QLabel(
                'Ghostscript will rewrite the PDF to a clean copy. '
                'The current file will be moved to the book\'s data folder '
                f'as <tt>{DIRTY_BACKUP_SUFFIX}</tt>.'
            )
            gs_note.setWordWrap(True)
            layout.addWidget(gs_note)

        layout.addSpacing(8)

        # ---- Buttons ----
        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)

        if corrupt_count:
            gs_available = bool(gs_path and shutil.which(gs_path))
            self._fix_btn = QPushButton('Fix with Ghostscript && Continue')
            self._fix_btn.setEnabled(gs_available)
            if not gs_available:
                self._fix_btn.setToolTip(
                    f'Ghostscript not found at {gs_path!r}. '
                    'Set the path in Preferences → Plugins → Embed ToC → Customize.'
                )
            self._fix_btn.clicked.connect(self._on_fix)
            btn_layout.addWidget(self._fix_btn)

        continue_btn = QPushButton('Continue Anyway')
        continue_btn.clicked.connect(self._on_continue)
        btn_layout.addWidget(continue_btn)

        cancel_btn = QPushButton('Cancel')
        cancel_btn.setDefault(True)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _on_fix(self):
        self.action = 'fix'
        self.accept()

    def _on_continue(self):
        self.action = 'continue'
        self.accept()


# ---------- Main dialog ----------

class EmbedTocDialog(QDialog):

    TAB_TEXT = 0
    TAB_TREE = 1

    def __init__(self, parent, pdf_path, book_title='Untitled'):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.book_folder = os.path.dirname(pdf_path)
        self.data_folder = os.path.join(self.book_folder, 'data')
        # Make sure the data folder exists; harmless if it already does.
        os.makedirs(self.data_folder, exist_ok=True)
        self.toc_path = os.path.join(self.data_folder, TOC_FILENAME)
        # The backup of the original PDF lives in data/ with suffix
        # '.original_pdf' (extensionless). Only ever written once.
        pdf_basename = os.path.basename(pdf_path)
        pdf_stem, _ = os.path.splitext(pdf_basename)
        self.pdf_stem = pdf_stem
        self.backup_path = os.path.join(
            self.data_folder, pdf_stem + ORIGINAL_BACKUP_SUFFIX)
        self.dirty_path = os.path.join(
            self.data_folder, pdf_stem + DIRTY_BACKUP_SUFFIX)
        self.book_title = book_title

        # Track which tab the user is leaving so we know which side to parse.
        self._current_tab = self.TAB_TEXT
        # Set during a programmatic tab switch to suppress re-entry into
        # the change handler.
        self._suppress_tab_handler = False

        self.setWindowTitle(f'Embed ToC — {book_title}')
        self.resize(900, 700)

        self._build_ui()
        self._load_initial_toc()
        self._restore_geometry()

    # ---- Geometry persistence ----

    def _restore_geometry(self):
        geom = gprefs.get('embed_pdf_toc_dialog_geometry')
        if geom:
            self.restoreGeometry(bytes(geom))

    def _save_geometry(self):
        gprefs['embed_pdf_toc_dialog_geometry'] = bytearray(self.saveGeometry())

    def done(self, result):
        self._autosave_toc()
        self._save_geometry()
        super().done(result)

    def _autosave_toc(self):
        '''Persist the current TOC to disk on any close path (including Cancel).
        Silently skips if the editor content can't be parsed.'''
        try:
            toc = self._current_toc()
            self._save_toc_file(toc)
        except (TocParseError, OSError):
            pass

    # ---- UI construction ----

    def _build_ui(self):
        layout = QVBoxLayout(self)

        header = QLabel(f'<b>PDF:</b> {self.pdf_path}')
        header.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header.setWordWrap(True)
        layout.addWidget(header)

        # --- Offset widget (tree tab only; created here so load code can
        # reference it before the tab is built).
        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-100000, 100000)
        self.offset_spin.setValue(0)
        self.offset_note = QLabel('')
        self.offset_note.setStyleSheet('color: gray;')

        # --- Tabs ---
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # Text tab
        self.text_edit = TocTextEdit()
        self.text_edit.setPlaceholderText(PLACEHOLDER)
        font = self.text_edit.font()
        font.setFamily('Menlo')   # falls back gracefully if unavailable
        font.setStyleHint(font.StyleHint.Monospace)
        self.text_edit.setFont(font)
        self.tabs.addTab(self._wrap_text_tab(), 'Text')

        # Tree tab
        self.tabs.addTab(self._wrap_tree_tab(), 'Tree')

        self.tabs.currentChanged.connect(self._on_tab_changed)

        # --- Bottom button bar ---
        bottom = QHBoxLayout()
        self.load_btn = QPushButton('Load from file…')
        self.load_btn.clicked.connect(self._on_load_from_file)
        bottom.addWidget(self.load_btn)

        self.clean_btn = QPushButton('Clean PDF')
        self.clean_btn.setToolTip(
            'Rewrite the PDF using Ghostscript to fix corrupt internal references.\n'
            'This may resolve issues where the table of contents appears correctly\n'
            'on desktop but is missing on e-reader devices (e.g. KOReader).'
        )
        self.clean_btn.clicked.connect(self._on_clean_pdf)
        bottom.addWidget(self.clean_btn)
        bottom.addStretch(1)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Close)
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).setText('Apply')
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._on_apply)
        self.button_box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        bottom.addWidget(self.button_box)
        layout.addLayout(bottom)

    def _wrap_text_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.text_edit)
        return w

    def _wrap_tree_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel('Global page offset:'))
        toolbar.addWidget(self.offset_spin)
        toolbar.addWidget(self.offset_note)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        toolbar.addWidget(sep)

        add_btn = QPushButton('Add entry')
        add_btn.clicked.connect(lambda: self.toc_tree.add_entry())
        toolbar.addWidget(add_btn)

        del_btn = QPushButton('Delete selected')
        del_btn.clicked.connect(lambda: self.toc_tree.delete_selected())
        toolbar.addWidget(del_btn)

        indent_btn = QPushButton('Indent →')
        indent_btn.setToolTip('Make each selected item a child of the item above it')
        indent_btn.clicked.connect(lambda: self.toc_tree.indent_selected())
        toolbar.addWidget(indent_btn)

        dedent_btn = QPushButton('← Dedent')
        dedent_btn.setToolTip('Move each selected item one level up')
        dedent_btn.clicked.connect(lambda: self.toc_tree.dedent_selected())
        toolbar.addWidget(dedent_btn)

        toolbar.addStretch(1)

        expand_btn = QPushButton('Expand all')
        expand_btn.clicked.connect(lambda: self.toc_tree.expandAll())
        toolbar.addWidget(expand_btn)

        collapse_btn = QPushButton('Collapse all')
        collapse_btn.clicked.connect(lambda: self.toc_tree.collapseAll())
        toolbar.addWidget(collapse_btn)

        v.addLayout(toolbar)

        self.toc_tree = TocTree()
        v.addWidget(self.toc_tree, 1)
        return w

    # ---- TOC load/save ----

    def _load_initial_toc(self):
        '''Read the saved TOC file if present. Populate text view; tree is
        populated lazily on first tab switch.

        Also handles a one-time migration from v0.1.0 layout: TOC and
        backup used to live next to the PDF rather than in data/.
        '''
        # Legacy migration: move <book>/toc -> <book>/data/toc if needed.
        legacy_toc = os.path.join(self.book_folder, TOC_FILENAME)
        if (os.path.isfile(legacy_toc)
                and not os.path.isfile(self.toc_path)):
            try:
                shutil.move(legacy_toc, self.toc_path)
            except OSError:
                pass

        # Legacy migration: move <book>.pdf.bak -> data/<stem>.original_pdf
        # if no current-format backup exists.
        legacy_bak = self.pdf_path + '.bak'
        if (os.path.isfile(legacy_bak)
                and not os.path.isfile(self.backup_path)):
            try:
                shutil.move(legacy_bak, self.backup_path)
            except OSError:
                pass

        if os.path.isfile(self.toc_path):
            try:
                with open(self.toc_path, 'r', encoding='utf-8') as f:
                    text = f.read()
            except OSError as e:
                error_dialog(self, "Couldn't read TOC file",
                             f'{self.toc_path}\n\n{e}', show=True)
                text = ''
            self.text_edit.setPlainText(text)
            # Initialize offset spin from saved file if possible.
            try:
                toc = parse_toc(text)
                self.offset_spin.setValue(toc.offset)
                self._update_offset_state(toc)
            except TocParseError:
                # Don't block the user; they'll see the error when they try
                # to switch tabs or apply.
                pass

    def _save_toc_file(self, toc):
        '''Write the current TOC to <book>/data/toc.'''
        text = serialize_toc(toc)
        with open(self.toc_path, 'w', encoding='utf-8') as f:
            f.write(text)

    # ---- Tab switching ----

    def _on_tab_changed(self, new_index):
        if self._suppress_tab_handler:
            self._current_tab = new_index
            return

        # We're leaving _current_tab and entering new_index. Sync.
        leaving = self._current_tab
        try:
            if leaving == self.TAB_TEXT:
                toc = self._toc_from_text()
            else:
                toc = self._toc_from_tree()
        except TocParseError as e:
            # Block the switch and revert.
            self._suppress_tab_handler = True
            self.tabs.setCurrentIndex(leaving)
            self._suppress_tab_handler = False
            self._show_parse_error(e)
            return

        # Push into the now-current side.
        if new_index == self.TAB_TEXT:
            self.text_edit.setPlainText(serialize_toc(toc))
        else:
            self.toc_tree.load(toc.entries)

        self.offset_spin.setValue(toc.offset)
        self._update_offset_state(toc)
        self._current_tab = new_index

    def _toc_from_text(self):
        # In the text view, the '# offset: N' directive is the source of
        # truth -- the spin box is hidden on this tab.
        return parse_toc(self.text_edit.toPlainText())

    def _toc_from_tree(self):
        entries = self.toc_tree.dump_entries()
        return ParsedToc(entries=entries, offset=self.offset_spin.value())

    def _current_toc(self):
        '''Pull the current TOC from whichever tab is active. Raises
        TocParseError on parse failure.'''
        if self._current_tab == self.TAB_TEXT:
            return self._toc_from_text()
        return self._toc_from_tree()

    def _update_offset_state(self, toc):
        '''Enable the offset spinbox iff all entries have positive integer
        pages. Update the note label to explain when disabled.'''
        if not toc.entries:
            self.offset_spin.setEnabled(False)
            self.offset_note.setText('(disabled: no entries yet)')
            return
        if all_pages_numeric(toc):
            self.offset_spin.setEnabled(True)
            self.offset_note.setText('')
        else:
            self.offset_spin.setEnabled(False)
            self.offset_note.setText(
                '(disabled: non-numeric page labels present)')

    # ---- Clean PDF ----

    def _on_clean_pdf(self):
        if not shutil.which(prefs['gs_path']):
            self._open_prefs_dialog()
            return
        if not question_dialog(
                self, 'Clean PDF with Ghostscript?',
                'This will rewrite the PDF using Ghostscript to fix corrupt internal '
                'references. The current file will be moved to the book\'s data folder '
                f'as <tt>{DIRTY_BACKUP_SUFFIX}</tt>.<br><br>'
                'Continue?'):
            return
        if self._run_gs_fix():
            info_dialog(
                self, 'PDF cleaned',
                'The PDF was successfully rewritten by Ghostscript.',
                show=True)

    def _open_prefs_dialog(self):
        '''Open an inline preferences dialog for the plugin so the user can
        set the Ghostscript path without leaving the embed dialog.'''
        from calibre_plugins.toc_bookmarker.config import ConfigWidget
        cw = ConfigWidget()
        dlg = QDialog(self)
        dlg.setWindowTitle('Embed PDF ToC — Preferences')
        layout = QVBoxLayout(dlg)
        layout.addWidget(cw)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cw.commit()

    # ---- Load from external file ----

    def _on_load_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Choose a TOC file', self.book_folder,
            'TOC files (toc *.toc *.txt);;All files (*)')
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
        except OSError as e:
            return error_dialog(self, "Couldn't read file", str(e), show=True)

        same_file = os.path.abspath(path) == os.path.abspath(self.toc_path)
        if not same_file:
            # Copy to the book folder, prompting on overwrite.
            if os.path.exists(self.toc_path):
                if not question_dialog(
                        self, 'Overwrite existing TOC?',
                        f'A TOC file already exists at\n\n{self.toc_path}\n\n'
                        'Replace it with the contents of the selected file?'):
                    return
            try:
                shutil.copyfile(path, self.toc_path)
            except OSError as e:
                return error_dialog(self, "Couldn't copy file", str(e), show=True)

        # Load into the text view; tree is repopulated on next tab switch.
        self.text_edit.setPlainText(text)
        try:
            toc = parse_toc(text)
            self.offset_spin.setValue(toc.offset)
            self._update_offset_state(toc)
            if self._current_tab == self.TAB_TREE:
                self.toc_tree.load(toc.entries)
        except TocParseError as e:
            self._show_parse_error(e)

    # ---- PDF health check / GS fix ----

    def _check_pdf_and_maybe_fix(self):
        '''
        Run a health check on the PDF and, if issues are found, show
        _PdfHealthDialog. Returns True if the apply should proceed
        (possibly after a GS fix), False if the user cancelled.
        '''
        try:
            health = check_pdf_health(self.pdf_path)
        except Exception:
            # If we can't even read the file, let _on_apply surface the error.
            return True

        if not health['has_toc'] and not health['corrupt_count']:
            return True

        dlg = _PdfHealthDialog(
            self,
            has_toc=health['has_toc'],
            corrupt_count=health['corrupt_count'],
            gs_path=prefs['gs_path'],
        )
        dlg.exec()

        if dlg.action == 'cancel':
            return False
        if dlg.action == 'fix':
            return self._run_gs_fix()
        return True  # 'continue'

    def _run_gs_fix(self):
        '''
        Run Ghostscript to rewrite self.pdf_path as a clean copy.
        The original is moved to self.dirty_path first; on failure it is
        restored and an error dialog is shown.
        Returns True on success, False on failure.
        '''
        gs_path = prefs['gs_path']
        if os.path.exists(self.dirty_path):
            if not question_dialog(
                    self, 'Dirty backup already exists',
                    f'A previous dirty backup already exists at\n\n{self.dirty_path}\n\n'
                    'Overwrite it with the current PDF?'):
                return False
            try:
                os.remove(self.dirty_path)
            except OSError as e:
                return not error_dialog(self, "Couldn't remove old dirty backup",
                                        str(e), show=True)

        try:
            shutil.move(self.pdf_path, self.dirty_path)
        except OSError as e:
            return not error_dialog(self, "Couldn't move PDF for GS fix",
                                    str(e), show=True)

        try:
            result = subprocess.run(
                [gs_path, '-dBATCH', '-dNOPAUSE', '-sDEVICE=pdfwrite',
                 f'-sOutputFile={self.pdf_path}', self.dirty_path],
                capture_output=True, text=True,
            )
        except OSError as e:
            shutil.move(self.dirty_path, self.pdf_path)
            return not error_dialog(
                self, "Couldn't launch Ghostscript",
                f'Command: {gs_path}\n\n{e}', show=True)

        if result.returncode != 0:
            shutil.move(self.dirty_path, self.pdf_path)
            return not error_dialog(
                self, 'Ghostscript failed',
                result.stderr or result.stdout or '(no output)', show=True)

        return True

    # ---- Apply / write ----

    def _on_apply(self):
        try:
            toc = self._current_toc()
        except TocParseError as e:
            return self._show_parse_error(e)

        if not toc.entries:
            return error_dialog(
                self, 'Nothing to write',
                'The TOC is empty. Add at least one entry, then try again.',
                show=True)

        if not self._check_pdf_and_maybe_fix():
            return

        apply_offset = all_pages_numeric(toc) and toc.offset != 0

        # Persist the TOC file before attempting the write -- it makes
        # iteration nicer if the write fails.
        try:
            self._save_toc_file(toc)
        except OSError as e:
            return error_dialog(self, "Couldn't save TOC file",
                                f'{self.toc_path}\n\n{e}', show=True)

        # Back up the original PDF -- but only once. If a backup already
        # exists, leave it alone (it represents the truly-original file).
        backup_existed_before = os.path.isfile(self.backup_path)
        if not backup_existed_before:
            try:
                shutil.copyfile(self.pdf_path, self.backup_path)
            except OSError as e:
                return error_dialog(self, "Couldn't back up PDF",
                                    f'{self.backup_path}\n\n{e}', show=True)

        # Run the write. Errors are surfaced and we return to the editor.
        try:
            write_bookmarks(self.pdf_path, toc, apply_offset)
        except MissingPageLabel as e:
            error_dialog(
                self, 'Page label not found',
                f'The page label <b>{e.label}</b> for entry '
                f'<b>{e.title}</b> was not found in this PDF.<br><br>'
                'Fix the entry and try again.',
                show=True)
            # The PDF wasn't actually modified (write_bookmarks validates
            # before writing, and the write is atomic). If we created the
            # backup just now, remove it so we don't leave a confusing
            # artifact behind for a run that didn't change anything.
            if not backup_existed_before:
                self._remove_backup_silently()
            return
        except BookmarkError as e:
            error_dialog(self, 'Bookmark write failed', str(e), show=True)
            if not backup_existed_before:
                self._remove_backup_silently()
            return
        except Exception as e:
            import traceback
            error_dialog(self, 'Bookmark write failed',
                         str(e), det_msg=traceback.format_exc(), show=True)
            if not backup_existed_before:
                self._remove_backup_silently()
            return

        self._show_post_write_dialog()

    def _remove_backup_silently(self):
        '''Remove the backup file if present. Used only when we created it
        during this run but the run failed without modifying the PDF.'''
        try:
            if os.path.exists(self.backup_path):
                os.remove(self.backup_path)
        except OSError:
            pass

    def _show_post_write_dialog(self):
        '''Modal dialog with Open PDF / Undo / Close.'''
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle('ToC embedded')
        box.setText('Table of contents embedded successfully.')
        open_btn = box.addButton('Open PDF', QMessageBox.ButtonRole.AcceptRole)
        undo_btn = box.addButton('Undo', QMessageBox.ButtonRole.DestructiveRole)
        close_btn = box.addButton('Close', QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(open_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked is open_btn:
            self._open_pdf_externally()
            # The post-write action is done; close the editor.
            self.accept()
        elif clicked is undo_btn:
            self._undo_write()
            # Stay in the editor so the user can adjust and retry.
        else:
            self.accept()

    def _undo_write(self):
        '''Restore the original PDF from the .original_pdf backup, then
        return to the editor. The backup file is preserved -- it always
        represents the truly-original PDF and is never modified after its
        initial creation.'''
        if not os.path.isfile(self.backup_path):
            return error_dialog(
                self, "Couldn't undo",
                'The original PDF backup is missing. Nothing to restore.',
                show=True)
        try:
            shutil.copyfile(self.backup_path, self.pdf_path)
        except OSError as e:
            return error_dialog(self, "Couldn't restore original", str(e), show=True)

    def _open_pdf_externally(self):
        '''Open the PDF with the OS default application.'''
        try:
            if sys.platform == 'darwin':
                subprocess.Popen(['open', self.pdf_path])
            elif sys.platform.startswith('win'):
                os.startfile(self.pdf_path)  # noqa: SIM115
            else:
                subprocess.Popen(['xdg-open', self.pdf_path])
        except Exception as e:
            error_dialog(self, "Couldn't open PDF",
                         f'Failed to launch the default PDF viewer:\n{e}',
                         show=True)

    # ---- Misc ----

    def _show_parse_error(self, err):
        details = ''
        if err.line_number is not None:
            details = f'Line {err.line_number}: {err.line_text!r}'
        error_dialog(self, "Couldn't parse TOC",
                     str(err), det_msg=details, show=True)
