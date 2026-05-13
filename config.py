import shutil

from calibre.utils.config import JSONConfig
from qt.core import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

prefs = JSONConfig('plugins/toc_bookmarker')
prefs.defaults['gs_path'] = 'gs'

_BORDER_VALID   = 'border: 2px solid #4caf50; border-radius: 4px; padding: 2px;'
_BORDER_INVALID = 'border: 2px solid #f44336; border-radius: 4px; padding: 2px;'


class ConfigWidget(QWidget):

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel('Ghostscript executable:'))
        self.gs_edit = QLineEdit(prefs['gs_path'])
        self.gs_edit.setPlaceholderText('gs  (or full path, e.g. /usr/local/bin/gs)')
        self.gs_edit.textChanged.connect(self._validate)
        row.addWidget(self.gs_edit)
        browse = QPushButton('Browse…')
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        note = QLabel(
            'Used to clean corrupt PDF cross-references before embedding a TOC. '
            'Leave as <tt>gs</tt> if Ghostscript is on your PATH.'
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()

        self._validate(self.gs_edit.text())

    def _validate(self, text):
        path = text.strip() or 'gs'
        resolved = shutil.which(path)
        if resolved:
            self.gs_edit.setStyleSheet(_BORDER_VALID)
            self.status_label.setText(f'<span style="color: #4caf50;">✓ Found: {resolved}</span>')
        else:
            self.gs_edit.setStyleSheet(_BORDER_INVALID)
            self.status_label.setText(
                f'<span style="color: #f44336;">✗ Not found: {path!r}. '
                'Install Ghostscript or provide the full path.</span>'
            )

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Select Ghostscript executable')
        if path:
            self.gs_edit.setText(path)

    def commit(self):
        prefs['gs_path'] = self.gs_edit.text().strip() or 'gs'
