from calibre.customize import InterfaceActionBase


class EmbedToc(InterfaceActionBase):
    '''
    Wrapper class for the Embed ToC plugin. Lives in __init__.py and
    MUST NOT import any GUI libraries here. The real plugin is in ui.py.
    '''
    name                    = 'Embed PDF ToC'
    description             = ('Embed a navigable table of contents into PDF '
                               'books in your library from a plain-text TOC.')
    supported_platforms     = ['windows', 'osx', 'linux']
    author                  = 'Rex'
    version                 = (0, 3, 3)
    minimum_calibre_version = (5, 0, 0)

    # module_path:class_name; loaded only in a GUI context.
    actual_plugin = 'calibre_plugins.toc_bookmarker.ui:EmbedTocAction'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.toc_bookmarker.config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.commit()
