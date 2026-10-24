import builtins
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pandas as pd
import requests

from mindsdb.integrations.libs.api_handler import APIResource
from mindsdb.integrations.utilities.sql_utils import FilterOperator
from mindsdb.utilities import log

logger = log.getLogger(__name__)

BASE_URL = "https://api.brevo.com/v3"
PAGE_SIZE = 50
MAX_RETRIES = 5
MAX_WORKERS = 7


def _get_with_backoff(url, headers, params=None, timeout=30, max_retries=MAX_RETRIES):
    """Send a GET request, retrying when Brevo rate-limits us.

    On an HTTP 429 the function sleeps for the number of seconds in the
    Retry-After header, or 2 ** attempt seconds if the header is missing, then
    tries again. Any other status code is returned straight away.

    Args:
        url: The full Brevo API URL to request.
        headers: HTTP headers for the request, including the api-key.
        params: Optional query-string parameters. Defaults to None.
        timeout: Seconds to wait for each attempt. Defaults to 30.
        max_retries: How many times to retry after a 429. Defaults to
            MAX_RETRIES.

    Returns:
        The requests.Response from the last attempt. It can still be a 429 if
        every retry was rate-limited, so the caller must check status_code.
    """
    for attempt in range(max_retries + 1):
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code != 429 or attempt == max_retries:
            return r
        wait = float(r.headers.get("Retry-After", 2**attempt))
        logger.warning(f"Brevo: rate limited on {url}, retrying in {wait}s")
        time.sleep(wait)
    return r


def _equals(conditions, column):
    """Find an equality filter on one column and mark it as handled.

    Setting `applied = True` tells MindsDB the handler already used this filter
    (by sending it to Brevo), so MindsDB does not filter the rows a second time.

    Args:
        conditions: The WHERE conditions MindsDB passed to the table, or None.
        column: The column name to look for.

    Returns:
        The value from the first `column = value` condition, or None if there
        is no such condition.
    """
    for c in conditions or []:
        if c.column == column and c.op == FilterOperator.EQUAL:
            c.applied = True
            return c.value
    return None


def _snake(name: str) -> str:
    """Convert a camelCase name to snake_case.

    Args:
        name: A camelCase string, for example "hardBounces".

    Returns:
        The snake_case version, for example "hard_bounces".
    """
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _total(stats, key):
    """Sum one statistic across the per-list entries of a campaign.

    Args:
        stats: The `campaignStats` list from a Brevo campaign.
        key: The statistic name, for example "sent".

    Returns:
        The sum, treating missing or null values as 0.
    """
    return sum(s.get(key, 0) or 0 for s in stats)


def _rate(part, whole):
    """Compute part / whole as a ratio rounded to 4 decimal places.

    Args:
        part: The numerator, for example the number of opens.
        whole: The denominator, for example the number delivered.

    Returns:
        The ratio between 0 and 1, or None if whole is 0 or missing, so a
        campaign with no sends shows NULL instead of causing a divide-by-zero.
    """
    return round(part / whole, 4) if whole else None


class CampaignsTable(APIResource):
    """Table of email campaigns, one row per campaign.

    Brevo reports statistics separately for each list a campaign was sent to,
    so the numeric columns are the sum across all of those lists.
    """

    columns = [
        "campaign_id",
        "campaign_name",
        "subject",
        "status",
        "sent_at",
        "sent",
        "delivered",
        "hard_bounces",
        "soft_bounces",
        "unique_opens",
        "opens_total",
        "unique_clicks",
        "clickers",
        "unsubscriptions",
        "complaints",
        "delivery_rate",
        "open_rate",
    ]

    def list(self, conditions=None, limit=None, sort=None, targets=None, **kwargs) -> pd.DataFrame:
        """Fetch email campaigns and their delivery statistics.

        Filters and sorting are applied by MindsDB after this method returns.

        Args:
            conditions: WHERE conditions from the query. Not pushed to Brevo.
            limit: Maximum number of rows to return. Defaults to None (all).
            sort: Requested ordering. Not used here.
            targets: Column names the query selected. Defaults to all columns.
            **kwargs: Extra arguments passed by MindsDB, ignored.

        Returns:
            A DataFrame with one row per campaign and the columns in
            CampaignsTable.columns, restricted to `targets` when given.

        Raises:
            RuntimeError: If Brevo returns a non-200 status code.
            ValueError: If no api_key was given when the handler was created.
        """
        headers = self.handler.connect()
        campaigns = []
        offset = 0
        while True:
            page = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(campaigns))
            if page <= 0:
                break

            r = _get_with_backoff(
                f"{BASE_URL}/emailCampaigns",
                headers,
                params={"limit": page, "offset": offset},
            )
            if r.status_code != 200:
                raise RuntimeError(f"Brevo returned {r.status_code}: {r.text[:300]}")

            body = r.json()
            batch = body.get("campaigns", [])
            if not batch:
                break

            campaigns.extend(batch)
            offset += len(batch)
            # `count` is the total number of campaigns, not the size of this page
            if offset >= body.get("count", offset):
                break

        rows = []
        for c in campaigns:
            # NOTE: globalStats is always zero here; the real numbers are per-list
            # in campaignStats, so sum across that array.
            stats = (c.get("statistics") or {}).get("campaignStats") or []

            sent, delivered = _total(stats, "sent"), _total(stats, "delivered")
            unique_opens = _total(stats, "uniqueViews")

            rows.append(
                {
                    "campaign_id": c.get("id"),
                    "campaign_name": c.get("name"),
                    "subject": c.get("subject"),
                    "status": c.get("status"),
                    "sent_at": c.get("sentDate"),
                    "sent": sent,
                    "delivered": delivered,
                    "hard_bounces": _total(stats, "hardBounces"),
                    "soft_bounces": _total(stats, "softBounces"),
                    "unique_opens": unique_opens,
                    "opens_total": _total(stats, "viewed"),
                    "unique_clicks": _total(stats, "uniqueClicks"),
                    "clickers": _total(stats, "clickers"),
                    "unsubscriptions": _total(stats, "unsubscriptions"),
                    "complaints": _total(stats, "complaints"),
                    "delivery_rate": _rate(delivered, sent),
                    "open_rate": _rate(unique_opens, delivered),
                }
            )

        df = pd.DataFrame(rows, columns=self.columns)
        if limit:
            df = df.head(limit)
        if targets:
            keep = [c for c in self.columns if c in targets]
            if keep:
                df = df[keep]
        return df

    def get_columns(self) -> builtins.list[str]:
        """Return the column names of the campaigns table.

        Returns:
            The names in CampaignsTable.columns.
        """
        return self.columns


def _fetch_contact_events(addr, headers, campaign_id, event_type, rows, limit):
    """Fetch one contact's activity and append the matching events to `rows`.

    Calls Brevo's contact endpoint with statistics enabled, then walks every
    event list in the response (opens, clicks, and so on). Events are skipped
    when they do not match `campaign_id` or `event_type`. If the request fails,
    a warning is logged and nothing is added.

    Args:
        addr: The contact's email address.
        headers: HTTP headers for the request, including the api-key.
        campaign_id: Keep only events from this campaign. None keeps all.
        event_type: Keep only this snake_case event type, for example
            "opened". None keeps all.
        rows: List that matching events are appended to, as dicts with the keys
            email, campaign_id, event_type and event_time. Modified in place.
        limit: Stop once `rows` holds this many entries. None means no limit.

    Returns:
        None. The results are written into `rows`.
    """
    r = _get_with_backoff(f"{BASE_URL}/contacts/{addr}", headers, params={"statistics": "true"})
    if r.status_code != 200:
        logger.warning(f"Brevo: skipping {addr}, got {r.status_code}")
        return

    stats = r.json().get("statistics") or {}
    for event_name, events in stats.items():
        if not isinstance(events, list):
            continue
        for e in events:
            if campaign_id is not None and e.get("campaignId") != int(campaign_id):
                continue
            if event_type is not None and _snake(event_name) != event_type:
                continue
            rows.append(
                {
                    "email": addr,
                    "campaign_id": e.get("campaignId"),
                    "event_type": _snake(event_name),
                    "event_time": e.get("eventTime"),
                }
            )
            if limit and len(rows) >= limit:
                return


def _fetch_events_for_contacts(contacts, headers, campaign_id, event_type, limit):
    """Fetch events for several contacts at the same time.

    Brevo only exposes events through a per-contact endpoint, so one call is
    needed per contact. Each call mostly waits on the network, so running
    MAX_WORKERS of them together is much faster than running them one by one.

    Every worker fills its own private list, and the lists are merged here in
    the same order as `contacts`, so the output is identical to a sequential
    run. Once `limit` rows are collected, calls that have not started yet are
    cancelled. Calls already in flight finish and their rows are discarded.

    Args:
        contacts: Contact dicts from Brevo, each with an "email" key.
        headers: HTTP headers for the requests, including the api-key.
        campaign_id: Keep only events from this campaign. None keeps all.
        event_type: Keep only this snake_case event type, for example
            "opened". None keeps all.
        limit: Stop once this many rows are collected. None means no limit.

    Returns:
        A list of event dicts with the keys email, campaign_id, event_type and
        event_time, in the order of `contacts`. It can hold more than `limit`
        rows, so the caller should trim it.

    Raises:
        Exception: Anything a worker raises is re-raised here, so a failed
            fetch is not silently dropped from the results.
    """

    def fetch_one(contact):
        out = []
        _fetch_contact_events(contact["email"], headers, campaign_id, event_type, out, limit)
        return out

    rows = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, c) for c in contacts]
        for future in futures:
            rows.extend(future.result())
            if limit and len(rows) >= limit:
                for pending in futures:
                    pending.cancel()
                break
    return rows


class ContactEventsTable(APIResource):
    """Table of contact activity, one row per contact per event.

    Use it to answer questions like "who opened this campaign". Brevo has no
    single endpoint for this, so rows are built from each contact's own
    statistics.
    """

    columns = ["email", "campaign_id", "event_type", "event_time"]

    def list(self, conditions=None, limit=None, sort=None, targets=None, **kwargs) -> pd.DataFrame:
        """Fetch contact events such as opens and clicks.

        With an `email = ...` filter this makes a single API call. Without one,
        it pages through every contact and makes one call per contact, which is
        slow and can hit rate limits on large audiences, so filter by email or
        use `limit` where possible.

        Args:
            conditions: WHERE conditions from the query. Equality filters on
                email, campaign_id and event_type are applied inside the
                handler; other conditions are applied by MindsDB afterwards.
            limit: Maximum number of rows to return. Defaults to None (all).
            sort: Requested ordering. Not used here.
            targets: Column names the query selected. Defaults to all columns.
            **kwargs: Extra arguments passed by MindsDB, ignored.

        Returns:
            A DataFrame with the columns in ContactEventsTable.columns,
            restricted to `targets` when given.

        Raises:
            RuntimeError: If listing contacts returns a non-200 status code.
            ValueError: If no api_key was given when the handler was created.
        """
        headers = self.handler.connect()

        # Events come from a per-contact endpoint, so filtering by email turns
        # this from N calls into one. Push it down whenever we can.
        email = _equals(conditions, "email")
        campaign_id = _equals(conditions, "campaign_id")
        event_type = _equals(conditions, "event_type")

        rows = []

        if email:
            _fetch_contact_events(email, headers, campaign_id, event_type, rows, limit)
        else:
            offset = 0
            while True:
                r = _get_with_backoff(
                    f"{BASE_URL}/contacts",
                    headers,
                    params={"limit": PAGE_SIZE, "offset": offset},
                )
                if r.status_code != 200:
                    raise RuntimeError(f"Brevo returned {r.status_code}: {r.text[:300]}")

                body = r.json()
                page = body.get("contacts", [])
                if not page:
                    break

                # Fetch this page's contacts in parallel, one page at a time,
                # so we can stop as soon as LIMIT is satisfied instead of
                # always fetching every page up front.
                rows.extend(_fetch_events_for_contacts(page, headers, campaign_id, event_type, limit))

                if limit and len(rows) >= limit:
                    break

                offset += len(page)

                # `count` is the total number of contacts, not the size of this page
                if offset >= body.get("count", offset):
                    break

        df = pd.DataFrame(rows, columns=self.columns)
        if limit:
            df = df.head(limit)
        if targets:
            keep = [c for c in self.columns if c in targets]
            if keep:
                df = df[keep]
        return df

    def get_columns(self) -> builtins.list[str]:
        """Return the column names of the contact_events table.

        Returns:
            The names in ContactEventsTable.columns.
        """
        return self.columns


def _contact_row(c):
    """Flatten one Brevo contact object into a row for the contacts table.

    Args:
        c: A contact dict as returned by Brevo.

    Returns:
        A dict keyed by the names in ContactsTable.columns.
    """
    attrs = c.get("attributes") or {}
    return {
        "contact_id": c.get("id"),
        "email": c.get("email"),
        "first_name": attrs.get("FIRSTNAME"),
        "last_name": attrs.get("LASTNAME"),
        "sms": attrs.get("SMS"),
        # a contact can be in several lists; SQL cells cannot hold a
        # list, so join the ids into a readable string
        "list_ids": ",".join(str(i) for i in (c.get("listIds") or [])),
        "email_blacklisted": c.get("emailBlacklisted"),
        "sms_blacklisted": c.get("smsBlacklisted"),
        "created_at": c.get("createdAt"),
        "modified_at": c.get("modifiedAt"),
    }


class ContactsTable(APIResource):
    """Table of subscribers, one row per contact.

    Brevo nests names and phone numbers inside an `attributes` object; the
    table flattens them into ordinary columns.
    """

    columns = [
        "contact_id",
        "email",
        "first_name",
        "last_name",
        "sms",
        "list_ids",
        "email_blacklisted",
        "sms_blacklisted",
        "created_at",
        "modified_at",
    ]

    def list(self, conditions=None, limit=None, sort=None, targets=None, **kwargs) -> pd.DataFrame:
        """Fetch contacts, paging through Brevo until all are read.

        Args:
            conditions: WHERE conditions from the query. An equality filter on
                email fetches that single contact, and one on list_id is
                pushed to Brevo's per-list endpoint; other conditions are
                applied by MindsDB afterwards.
            limit: Maximum number of rows to return. Defaults to None (all).
            sort: Requested ordering. Not used here.
            targets: Column names the query selected. Defaults to all columns.
            **kwargs: Extra arguments passed by MindsDB, ignored.

        Returns:
            A DataFrame with the columns in ContactsTable.columns, restricted
            to `targets` when given. list_ids is a comma-separated string
            because a SQL cell cannot hold a list.

        Raises:
            RuntimeError: If Brevo returns a non-200 status code.
            ValueError: If no api_key was given when the handler was created.
        """
        headers = self.handler.connect()

        # Brevo can fetch one contact directly by email, so an email filter is
        # one request instead of paging through every contact.
        email = _equals(conditions, "email")
        if email:
            r = _get_with_backoff(f"{BASE_URL}/contacts/{quote(str(email), safe='')}", headers)
            if r.status_code == 404:
                rows = []
            elif r.status_code == 200:
                rows = [_contact_row(r.json())]
            else:
                raise RuntimeError(f"Brevo returned {r.status_code}: {r.text[:300]}")
            df = pd.DataFrame(rows, columns=self.columns)
            if targets:
                keep = [c for c in self.columns if c in targets]
                if keep:
                    df = df[keep]
            return df

        # Brevo has a dedicated endpoint for one list, so a list_id filter is
        # cheaper to push down than to apply locally afterwards.
        list_id = _equals(conditions, "list_id")
        path = f"/contacts/lists/{list_id}/contacts" if list_id else "/contacts"

        rows = []
        offset = 0
        while True:
            page = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(rows))
            if page <= 0:
                break

            r = _get_with_backoff(
                f"{BASE_URL}{path}",
                headers,
                params={"limit": page, "offset": offset},
            )
            if r.status_code != 200:
                raise RuntimeError(f"Brevo returned {r.status_code}: {r.text[:300]}")

            body = r.json()
            contacts = body.get("contacts", [])
            if not contacts:
                break

            rows.extend(_contact_row(c) for c in contacts)

            offset += len(contacts)
            if offset >= body.get("count", offset):
                break

        df = pd.DataFrame(rows, columns=self.columns)
        if targets:
            keep = [c for c in self.columns if c in targets]
            if keep:
                df = df[keep]
        return df

    def get_columns(self) -> builtins.list[str]:
        """Return the column names of the contacts table.

        Returns:
            The names in ContactsTable.columns.
        """
        return self.columns


class ListsTable(APIResource):
    """Table of contact lists, one row per list.

    Brevo calls them lists in the API and audiences in its web interface.
    """

    columns = [
        "list_id",
        "name",
        "folder_id",
        "total_subscribers",
        "unique_subscribers",
        "total_blacklisted",
    ]

    def list(self, conditions=None, limit=None, sort=None, targets=None, **kwargs) -> pd.DataFrame:
        """Fetch contact lists, paging through Brevo until all are read.

        Args:
            conditions: WHERE conditions from the query. Not pushed to Brevo.
            limit: Maximum number of rows to return. Defaults to None (all).
            sort: Requested ordering. Not used here.
            targets: Column names the query selected. Defaults to all columns.
            **kwargs: Extra arguments passed by MindsDB, ignored.

        Returns:
            A DataFrame with the columns in ListsTable.columns, restricted to
            `targets` when given.

        Raises:
            RuntimeError: If Brevo returns a non-200 status code.
            ValueError: If no api_key was given when the handler was created.
        """
        headers = self.handler.connect()

        rows = []
        offset = 0
        while True:
            page = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(rows))
            if page <= 0:
                break

            r = _get_with_backoff(
                f"{BASE_URL}/contacts/lists",
                headers,
                params={"limit": page, "offset": offset},
            )
            if r.status_code != 200:
                raise RuntimeError(f"Brevo returned {r.status_code}: {r.text[:300]}")

            body = r.json()
            lists = body.get("lists", [])
            if not lists:
                break

            for item in lists:
                rows.append(
                    {
                        "list_id": item.get("id"),
                        "name": item.get("name"),
                        "folder_id": item.get("folderId"),
                        "total_subscribers": item.get("totalSubscribers"),
                        "unique_subscribers": item.get("uniqueSubscribers"),
                        "total_blacklisted": item.get("totalBlacklisted"),
                    }
                )

            offset += len(lists)
            if offset >= body.get("count", offset):
                break

        df = pd.DataFrame(rows, columns=self.columns)
        if targets:
            keep = [c for c in self.columns if c in targets]
            if keep:
                df = df[keep]
        return df

    def get_columns(self) -> builtins.list[str]:
        """Return the column names of the lists table.

        Returns:
            The names in ListsTable.columns.
        """
        return self.columns
