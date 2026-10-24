# Brevo Handler

Brevo handler for MindsDB provides interfaces to connect to [Brevo](https://www.brevo.com/)
(formerly Sendinblue) via its Marketing API and pull email marketing data into MindsDB.

## Brevo Handler Implementation

This handler was implemented using the Brevo Marketing API v3 and the standard
Python `requests` library. No vendor SDK is required.

All endpoints are served from a single host, `https://api.brevo.com/v3`, and the
API key is sent in an `api-key` request header.

## Brevo Handler Initialization

The Brevo handler is initialized with the following required parameter:

* `api_key`: an API key generated in Brevo under **SMTP & API → API Keys**.

## Implemented Features

* `contacts` table — subscribers, with the nested `attributes` object flattened
  into columns
* `lists` table — audiences and their subscriber counts
* `campaigns` table — email campaigns with send status and delivery statistics
  (sent, delivered, bounces, opens, clicks, unsubscriptions, complaints), summed
  across every list a campaign was sent to
* `contact_events` table — per-contact activity (opens, clicks, and so on),
  built from each contact's own statistics

Collection endpoints are paginated with `limit` and `offset`. The `count` field
in each response is the **total** number of records, not the number returned, so
it is used as the stopping condition when fetching all pages.

## TODO

* Support `INSERT` on `contacts` so subscribers can be added from MindsDB
* Push more filters down to the API instead of filtering locally

All tables are read-only. Equality filters on `contacts.email`, `contacts.list_id`
and on `contact_events.email`, `campaign_id` and `event_type` are sent to Brevo
instead of being applied locally.

## Usage

To connect your Brevo account to MindsDB, use the below `CREATE DATABASE` statement:

```sql
CREATE DATABASE brevo_src
WITH ENGINE = 'brevo',
PARAMETERS = {
  "api_key": "xkeysib-your-key-here"
};
```

Query your contacts:

```sql
SELECT * FROM brevo_src.contacts;
```

Apply filters and limits:

```sql
SELECT email, first_name, last_name
FROM brevo_src.contacts
WHERE list_id = 2
LIMIT 10;
```

List your audiences:

```sql
SELECT list_id, name, unique_subscribers
FROM brevo_src.lists;
```

Join Brevo contacts against customer records held in another datasource, to see
which paying customers are actually subscribed:

```sql
SELECT c.name, c.email, c.total_spent, b.email_blacklisted
FROM postgres_src.public.customers AS c
JOIN brevo_src.contacts AS b
  ON c.email = b.email
ORDER BY c.total_spent DESC;
```

Campaign performance:

```sql
SELECT campaign_name, sent, delivered, open_rate
FROM brevo_src.campaigns
ORDER BY open_rate DESC;
```

Who opened a campaign:

```sql
SELECT email, event_time
FROM brevo_src.contact_events
WHERE campaign_id = 1 AND event_type = 'opened'
LIMIT 50;
```

## Notes

**`contact_events` can be slow.** Brevo only exposes events per contact. With an
`email = ...` filter this is one API call; without it, the handler makes one call
per contact. Filter by email or use `LIMIT` on large audiences.

**Authorised IP addresses.** Brevo can restrict API access to an allowlist of IP
addresses. If requests fail with HTTP 401 and a message naming an
"unrecognised IP address", the key is valid but the caller's address is not
approved — add it at
[app.brevo.com/security/authorised_ips](https://app.brevo.com/security/authorised_ips).
Home connections are often assigned a new address every day or two, so this can
reappear after it was working. `check_connection` surfaces the message from
Brevo unchanged so this is distinguishable from a bad key.

**A contact can belong to several lists.** The API returns `listIds` as an array.
Filtering by `list_id` matches a contact that belongs to that list, among others.
