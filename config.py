from calibre.utils.config import JSONConfig
from qt.core import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QWidget,
)

prefs = JSONConfig('plugins/toc_bookmarker')
prefs.defaults['gs_path'] = 'gs'


class ConfigWidget(QWidget):

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel('Ghostscript executable:'))
        self.gs_edit = QLineEdit(prefs['gs_path'])
        self.gs_edit.setPlaceholderText('gs  (or full path, e.g. /usr/local/bin/gs)')
        row.addWidget(self.gs_edit)
        browse = QPushButton('Browse…')
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

        note = QLabel(
            'Used to clean corrupt PDF cross-references before embedding a TOC. '
            'Leave as <tt>gs</tt> if Ghostscript is on your PATH.'
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Select Ghostscript executable')
        if path:
            self.gs_edit.setText(path)

    def commit(self):
        prefs['gs_path'] = self.gs_edit.text().strip() or 'gs'
