import unittest
from unittest.mock import MagicMock, patch

from mindsdb.integrations.handlers.brevo_handler.brevo_handler import BrevoHandler
from mindsdb.integrations.handlers.brevo_handler.brevo_tables import (
    CampaignsTable,
    ContactEventsTable,
    ContactsTable,
    ListsTable,
    _get_with_backoff,
)
from mindsdb.integrations.utilities.sql_utils import FilterOperator


def _response(status_code, json_body=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.text = text
    r.headers = headers or {}
    return r


class BrevoHandlerTest(unittest.TestCase):
    def setUp(self):
        self.handler = BrevoHandler("test_brevo", connection_data={"api_key": "fake-key"})

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_handler.requests.get")
    def test_check_connection_success(self, mock_get):
        mock_get.return_value = _response(200, {"email": "me@example.com"})
        response = self.handler.check_connection()
        self.assertTrue(response.success)

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_handler.requests.get")
    def test_check_connection_failure_passes_brevo_message(self, mock_get):
        mock_get.return_value = _response(401, text="unauthorized IP")
        response = self.handler.check_connection()
        self.assertFalse(response.success)
        self.assertIn("unauthorized IP", response.error_message)

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_contacts_list(self, mock_get):
        mock_get.side_effect = [
            _response(
                200,
                {
                    "contacts": [
                        {
                            "id": 1,
                            "email": "a@example.com",
                            "attributes": {"FIRSTNAME": "A"},
                            "listIds": [1, 2],
                            "emailBlacklisted": False,
                            "smsBlacklisted": False,
                            "createdAt": "2024-01-01",
                            "modifiedAt": "2024-01-02",
                        }
                    ],
                    "count": 1,
                },
            ),
        ]
        table = ContactsTable(self.handler)
        df = table.list()
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["email"], "a@example.com")
        self.assertEqual(df.iloc[0]["list_ids"], "1,2")

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_contact_events_skips_failed_contact(self, mock_get):
        mock_get.side_effect = [
            _response(200, {"contacts": [{"email": "a@example.com"}], "count": 1}),
            _response(404, text="not found"),
        ]
        table = ContactEventsTable(self.handler)
        df = table.list(conditions=[])
        self.assertEqual(len(df), 0)

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_contacts_pagination_uses_total_count(self, mock_get):
        mock_get.side_effect = [
            _response(200, {"contacts": [{"email": "a@example.com"}, {"email": "b@example.com"}], "count": 3}),
            _response(200, {"contacts": [{"email": "c@example.com"}], "count": 3}),
        ]
        df = ContactsTable(self.handler).list()
        self.assertEqual(list(df["email"]), ["a@example.com", "b@example.com", "c@example.com"])
        self.assertEqual(mock_get.call_args_list[1].kwargs["params"]["offset"], 2)

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_contacts_email_filter_hits_single_contact(self, mock_get):
        mock_get.return_value = _response(200, {"id": 1, "email": "a@example.com", "listIds": [1]})
        condition = MagicMock(column="email", op=FilterOperator.EQUAL, value="a@example.com")
        df = ContactsTable(self.handler).list(conditions=[condition])
        self.assertEqual(len(df), 1)
        self.assertTrue(condition.applied)
        self.assertIn("/contacts/a%40example.com", mock_get.call_args.args[0])

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_contacts_error_raises(self, mock_get):
        mock_get.return_value = _response(500, text="boom")
        with self.assertRaises(RuntimeError):
            ContactsTable(self.handler).list()

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_lists_list(self, mock_get):
        mock_get.return_value = _response(
            200, {"lists": [{"id": 2, "name": "News", "folderId": 1, "totalSubscribers": 5}], "count": 1}
        )
        df = ListsTable(self.handler).list()
        self.assertEqual(df.iloc[0]["name"], "News")
        self.assertEqual(df.iloc[0]["total_subscribers"], 5)

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_campaigns_sum_stats_across_lists_and_paginate(self, mock_get):
        def campaign(cid):
            return {
                "id": cid,
                "name": f"c{cid}",
                "statistics": {
                    "campaignStats": [
                        {"sent": 6, "delivered": 5, "uniqueViews": 2},
                        {"sent": 4, "delivered": 3, "uniqueViews": 2},
                    ]
                },
            }

        mock_get.side_effect = [
            _response(200, {"campaigns": [campaign(1)], "count": 2}),
            _response(200, {"campaigns": [campaign(2)], "count": 2}),
        ]
        df = CampaignsTable(self.handler).list()
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]["sent"], 10)
        self.assertEqual(df.iloc[0]["delivered"], 8)
        self.assertEqual(df.iloc[0]["open_rate"], 0.5)

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_campaign_with_no_sends_has_null_rates(self, mock_get):
        mock_get.return_value = _response(200, {"campaigns": [{"id": 1, "name": "draft"}], "count": 1})
        df = CampaignsTable(self.handler).list()
        self.assertTrue(df.iloc[0]["open_rate"] is None or df.isna().iloc[0]["open_rate"])

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_contact_events_email_filter(self, mock_get):
        mock_get.return_value = _response(
            200,
            {"statistics": {"opened": [{"campaignId": 1, "eventTime": "t1"}, {"campaignId": 2, "eventTime": "t2"}]}},
        )
        condition = MagicMock(column="campaign_id", op=FilterOperator.EQUAL, value=1)
        email = MagicMock(column="email", op=FilterOperator.EQUAL, value="a@example.com")
        df = ContactEventsTable(self.handler).list(conditions=[email, condition])
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["event_type"], "opened")

    def test_missing_api_key_raises(self):
        with self.assertRaises(ValueError):
            BrevoHandler("x", connection_data={}).connect()


class BackoffTest(unittest.TestCase):
    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.time.sleep")
    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_retries_on_429_then_succeeds(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            _response(429, headers={"Retry-After": "1"}),
            _response(200, {"ok": True}),
        ]
        r = _get_with_backoff("https://api.brevo.com/v3/contacts/a@example.com", {})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(1.0)

    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.time.sleep")
    @patch("mindsdb.integrations.handlers.brevo_handler.brevo_tables.requests.get")
    def test_gives_up_after_max_retries(self, mock_get, mock_sleep):
        mock_get.return_value = _response(429)
        r = _get_with_backoff("https://api.brevo.com/v3/contacts", {}, max_retries=2)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(mock_get.call_count, 3)


if __name__ == "__main__":
    unittest.main()

# python -m unittest mindsdb.integrations.handlers.brevo_handler.tests.test_brevo_handler -v
