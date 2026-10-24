from mindsdb.integrations.libs.const import HANDLER_SUPPORT_LEVEL, HANDLER_TYPE

from .__about__ import __description__ as description
from .__about__ import __version__ as version
from .connection_args import connection_args, connection_args_example

try:
    from .brevo_handler import BrevoHandler as Handler

    import_error = None
except Exception as e:
    Handler = None
    import_error = e

title = "Brevo"
name = "brevo"
type = HANDLER_TYPE.DATA
icon_path = "icon.svg"
support_level = HANDLER_SUPPORT_LEVEL.COMMUNITY

__all__ = [
    "Handler",
    "connection_args",
    "connection_args_example",
    "description",
    "icon_path",
    "import_error",
    "name",
    "support_level",
    "title",
    "type",
    "version",
]
