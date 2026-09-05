# Recovering an interrupted announcement

The engine saves `announcement_state` in the article's frontmatter **before**
sending a creation request. This engine-owned, single-line JSON value holds
each provider's creation key, original payload, start time and any received
response. It contains no credentials. Mastodon stores a credential fingerprint
to prevent accidentally retrying on another account; Bluesky stores the DID.
The state is removed when every enabled provider has coordinates.

Each checkpoint atomically replaces the Markdown file. Concurrent ingestion
and announcement runs lock the content directory, so two publishers cannot
reserve different keys for the same article. Keep the content directory on a
local filesystem that supports advisory locks.

## Normal retries

Re-run publishing normally. The engine reuses the original payload and key,
even if the article's title or summary has since changed. Successful creation
responses are saved before bookmarking or moving on to the next provider.
Permanent HTTP errors are not automatically retried within a request; network
errors, HTTP 408/429 and 5xx responses receive bounded backoff.

Bluesky reserves a TID record key and reads that exact record on the PDS before
creating it. If a creation response is lost, it reads the same key again. A
matching record supplies the missing coordinates; different content at that
key is an error and is never overwritten.

Mastodon uses the persisted key as `Idempotency-Key`. Its API documents storage
for **up to one hour**, so the engine stops retrying an unresolved creation
after 50 minutes, leaving a margin. A saved successful response can still be
recovered after that deadline without another creation request. An unresolved
older request stays pending rather than risking another announcement.

## An unresolved Mastodon request is too old

Check the intended Mastodon account for the original announcement first.

- If it exists, copy its host and status ID into the article as
  `mastodon_host: mastodon.example` and `mastodon_id: "123456789"`.
  Keep `announce: pending` if another provider is still missing. The next run
  skips Mastodon and settles the remaining state.
- If you have verified that it was never published, remove only the
  `"Mastodon"` entry from the `announcement_state` JSON object. Keep the other
  provider's entry intact. The next run reserves a fresh key and sends the
  announcement using the article's current title and summary.

Do not remove the state merely to clear an error: the remote post may already
exist. Use the same recovery process if Mastodon credentials changed during a
pending attempt or a permanent validation error requires correcting the saved
payload. Do not hand-edit the saved payload while retaining its key.

The engine does not automatically search the Mastodon account for an old
uncertain request: posting credentials need not grant account-reading access,
and a text match is not a reliable creation identifier.

## Verification

Run the offline regression checks from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

The tests simulate response loss and process restarts, verify that permanent
errors do not retry, and check that failed checkpoints preserve the article.
They never contact a model or social network.

Protocol references:

- [Mastodon status creation and idempotency](https://docs.joinmastodon.org/methods/statuses/#headers)
- [AT Protocol createRecord](https://github.com/bluesky-social/atproto/blob/main/lexicons/com/atproto/repo/createRecord.json)
- [AT Protocol getRecord](https://github.com/bluesky-social/atproto/blob/main/lexicons/com/atproto/repo/getRecord.json)
- [AT Protocol TIDs](https://atproto.com/specs/tid)
