import requests

from mindsdb.integrations.libs.api_handler import APIHandler
from mindsdb.integrations.libs.response import HandlerStatusResponse as StatusResponse
from mindsdb.utilities import log

from .brevo_tables import CampaignsTable, ContactEventsTable, ContactsTable, ListsTable

logger = log.getLogger(__name__)

BASE_URL = "https://api.brevo.com/v3"


class BrevoHandler(APIHandler):
    """Connects MindsDB to Brevo (email marketing) through its REST API.

    Exposes four read-only tables: campaigns, contact_events, contacts and lists.
    """

    name = "brevo"

    def __init__(self, name: str = None, **kwargs):
        """Create the handler and register its tables.

        No network call is made here; the API key is only checked when a query
        runs or `check_connection` is called.

        Args:
            name: The name of this data source in MindsDB. Defaults to None.
            **kwargs: Handler options. `connection_data` should be a dict with
                the `api_key` for the Brevo account.
        """
        super().__init__(name)
        self.connection_data = kwargs.get("connection_data", {}) or {}
        self._register_table("campaigns", CampaignsTable(self))
        self._register_table("contact_events", ContactEventsTable(self))
        self._register_table("contacts", ContactsTable(self))
        self._register_table("lists", ListsTable(self))

    def connect(self):
        """Build the HTTP headers used for every Brevo request.

        Brevo has no persistent connection, so this only reads the API key and
        marks the handler as connected. No network call happens here.

        Returns:
            A dict with the `api-key` and `accept` headers.

        Raises:
            ValueError: If `api_key` is missing from the connection data.
        """
        api_key = self.connection_data.get("api_key")
        if not api_key:
            raise ValueError("The 'api_key' parameter is required to connect to Brevo.")
        self.is_connected = True
        return {"api-key": api_key, "accept": "application/json"}

    def check_connection(self) -> StatusResponse:
        """Check that the API key works by calling Brevo's /account endpoint.

        Brevo's own error text is passed through on failure, because a 401 is
        often caused by the account's IP allowlist rather than a bad key.

        Returns:
            A StatusResponse with `success` set to True on HTTP 200. Otherwise
            `success` is False and `error_message` explains why. This method
            does not raise; all errors are reported through the response.
        """
        response = StatusResponse(False)
        try:
            r = requests.get(f"{BASE_URL}/account", headers=self.connect(), timeout=30)
            if r.status_code == 200:
                response.success = True
            else:
                # Pass Brevo's own message through - a 401 here is often the IP
                # allowlist, not a bad key, and the text says which.
                response.error_message = f"Brevo returned {r.status_code}: {r.text[:300]}"
                self.is_connected = False
        except Exception as e:
            logger.error(f"Error connecting to Brevo: {e}")
            response.error_message = str(e)
            self.is_connected = False
        return response
