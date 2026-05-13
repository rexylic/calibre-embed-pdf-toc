'''
InterfaceAction subclass for Embed ToC. This is what Calibre's toolbar
and menu customization UI will see and let the user place wherever they want.

By default we DON'T add the action to any toolbar or menu -- the user wires
it up themselves in Preferences -> Toolbars & menus.
'''

import os

from calibre.gui2 import error_dialog
from calibre.gui2.actions import InterfaceAction


class EmbedTocAction(InterfaceAction):

    name = 'Embed PDF ToC'

    # (display name, icon path or None, status tip, keyboard shortcut)
    # Keyboard shortcut left as None -- user can set one if they want.
    action_spec = ('Embed ToC', None,
                   'Embed a navigable table of contents into the selected PDF', None)

    # The action targets the currently selected book(s) in the library.
    action_type = 'current'

    # Skip the device-side context menu; this only makes sense in the library.
    dont_add_to = frozenset(['context-menu-device'])

    # Don't place automatically; the user picks where it goes.
    auto_repeat = False

    def initialization_complete(self):
        self.gui.library_view.context_menu.addSeparator()
        self.gui.library_view.context_menu.addAction(self.qaction)

    def genesis(self):
        # Try to load a custom icon; fall back to a builtin so the action
        # is never icon-less.
        icon = get_icons('images/icon.png', 'Embed PDF ToC')  # noqa: F821
        if icon and not icon.isNull():
            self.qaction.setIcon(icon)
        self.qaction.triggered.connect(self.run_bookmarker)

    def run_bookmarker(self):
        # Import GUI deps lazily so __init__.py stays GUI-free.
        from calibre_plugins.toc_bookmarker.dialog import EmbedTocDialog

        gui = self.gui
        rows = gui.library_view.selectionModel().selectedRows()
        if not rows:
            return error_dialog(
                gui, 'No book selected',
                'Select a PDF book in your library, then try again.',
                show=True)
        if len(rows) > 1:
            return error_dialog(
                gui, 'Select one book',
                'Embed ToC works on one book at a time. '
                'Please select a single PDF.',
                show=True)

        book_id = gui.library_view.model().id(rows[0])
        db = gui.current_db.new_api
        fmts = {f.upper() for f in (db.formats(book_id) or ())}
        if 'PDF' not in fmts:
            return error_dialog(
                gui, 'No PDF format',
                'The selected book has no PDF format. '
                'Embed ToC only supports PDF.',
                show=True)

        pdf_path = db.format_abspath(book_id, 'PDF')
        if not pdf_path or not os.path.isfile(pdf_path):
            return error_dialog(
                gui, 'PDF not accessible',
                "Couldn't locate the PDF file on disk for this book.",
                show=True)

        title = db.field_for('title', book_id) or 'Untitled'
        dlg = EmbedTocDialog(gui, pdf_path, book_title=title)
        dlg.exec()
