# postbag

**A newsletter engine you host yourself, shaped as a library.** You own the
Postgres. An email service provider owns the SMTP. postbag owns the awkward
part in between — subscribers, suppression, an idempotent send log, drip
sequences, compliant headers, and a static archive.

Named for the carrier's postbag — the one sack that holds a whole round's
letters. That is what this engine is: the single place a list's outgoing mail
and the record of what already went out both live.

> **Stage: new.** Version 0.1.0. Built to run its first production list, and that is
> the whole of its track record. There are no users to quote and no scale to
> claim. What it does have is a test suite that mutation-checks the parts that
> would be expensive to get wrong — suppression at the wire, the approval gate,
> the send cap, token verification — and a boundary test proving the package
> carries no reference to the application it was extracted from.

## Why this exists

Hosted newsletter tools price per subscriber and gate the useful features per
plan: drip automations behind one tier, tags behind another. A self-run engine
costs whatever your ESP charges per send, and the features are just code. If
your list is small and your sends are few, hosted is probably still the right
call — this is for the case where you would rather own the data and the
behaviour.

## What makes it different

- **A library, not a service.** No web UI, no daemon, no container to run. You
  import it and call it from whatever already runs your background jobs.
- **No database driver.** `postbag.ddl` hands you `CREATE TABLE` text;
  `postbag.store` builds parameterized SQL. Your application decides when, and
  in which transaction, any of it executes. The engine cannot open a connection
  you did not give it.
- **Idempotency is a UNIQUE index**, not application logic. `newsletter_sends`
  is keyed on `(subscriber_id, message_key)`. Two racing ticks, a retried job, a
  redelivered queue message — all of them collide in Postgres and lose.
- **Drips are markdown files, and `approved: true` is the consent seam.** A step
  auto-sends only when its front matter says so. The approval is a file edit and
  a commit: reviewable, revertible, attributable to a person.
- **A per-tick send cap.** The tick refuses, loudly, to send more than
  `subscribers × approved steps` messages in one pass — the DERIVED legitimate
  maximum, not a chosen threshold, so it cannot stall on legitimate work and
  fires only when something upstream has started duplicating. A drip bug's
  worst case is repeat-mailing real people; the cap turns that into one alert
  and zero delivered mistakes.
- **Suppression is enforced at the wire, and fails closed.** The last thing
  before the HTTP request drops any recipient on the suppression set. There is a
  test that inspects the outgoing request body to prove it.
- **No open tracking. No click tracking. No pixels.** Not a setting — there is
  no code to enable. The archive pages it generates load no external assets for
  the same reason.
- **One-click unsubscribe done properly.** `List-Unsubscribe` and
  `List-Unsubscribe-Post` on every message, a postal address in both the HTML
  and the plain-text part, and unsubscribe tokens that are HMAC-signed,
  purpose-scoped, constant-time verified, and never expire.
- **MIT licensed.** No open-core tier, no license change planned.

## Install

Not on PyPI — and deliberately not `pip install postbag`, which is a
DIFFERENT, unrelated package that already owns the name there. Install from
source:

```sh
git clone https://github.com/solozerolabs/postbag
uv add --editable ./postbag   # or: pip install -e ./postbag
```

## Quickstart

Create the tables — in your own migration, with the text frozen into it:

```python
from postbag import ddl

for statement in ddl.statements("app"):   # your schema name
    print(statement)
```

Build the SQL, run it with your own driver:

```python
from postbag import store

query = store.upsert_subscriber("app", list_id=LIST_ID, email="reader@example.com", source="signup")
await my_connection.execute(query.sql, query.params)
```

Work out who is due, then send:

```python
from postbag.drip import load_sequence, plan_tick
from postbag.render import render_email
from postbag.transport_resend import Message, send_batch

sequence = load_sequence(Path("content/drip"))
due = plan_tick(subscribers, sends, sequence, now)        # raises SendCapExceeded on a runaway

messages = []
for item in due:
    if not await store.claim_send(execute, "app", subscriber_id=item.subscriber.id,
                                  message_key=item.message_key):
        continue                                          # the database says it already went
    html_body, text_body = render_email(
        item.step.body,
        footer="Your Company, 1 Example St. You are getting this because you signed up.",
        unsubscribe_url=unsubscribe_link(item.subscriber.id),
    )
    messages.append(Message(item.subscriber.email, item.step.subject, html_body, text_body,
                            unsubscribe_link(item.subscriber.id)))

outcome = await send_batch(messages, suppressed_addresses, from_address="news@example.com",
                           api_key=RESEND_KEY, client=client, idempotency_key=tick_id)
```

`send_batch` returns a tri-state `SendOutcome` — `SENT`, `REJECTED`, `UNKNOWN` —
rather than a boolean, because a timeout or a 5xx is not proof of
Only a definite refusal (4xx) releases the claim, via `store.release_send`, so a
later pass retries. An ambiguous outcome — a timeout, a 5xx — keeps it: the
provider may already have accepted the work, and a duplicate cannot be taken
back.

A drip step is just a file:

```markdown
---
key: what-it-is
day: 2
subject: What this is, in one minute
approved: false
---
Thanks for signing up. Here is the short version.

- The first thing it does
- The second thing it does
```

The filename must be `02-what-it-is.md` — the front-matter `key` has to equal
the filename stem after the `NN-` prefix, and the loader refuses the file
otherwise. That is not pedantry: the key is half of the database's uniqueness
constraint, so a renamed key is a new message that re-mails the whole list.

## Command line

```sh
postbag validate-drip content/drip                     # pre-commit gate; exit 1 on any problem
postbag render-weekly --entries changelog.json --since 2026-08-01 --product Widgets
postbag export-archive --sends sends.json --messages-dir content/drip --out-dir site/archive
```

There are deliberately no database subcommands and nothing that sends. The host
owns the credentials and the transaction boundary; a CLI with its own connection
string is a `DATABASE_URL` typo away from a very bad afternoon.

## What it does not do

No web UI. No SMTP server — bring Resend or another ESP. No open/click
tracking, ever. No archive hosting (it generates the HTML; you serve it). No
scheduler — call `plan_tick` from the job runner you already have. No
double opt-in flow yet.

## Transports

Resend today. The transport is one module with one function; the outcome
mapping and the batching rules are the parts worth copying if you add another.

## License

MIT. Copyright (c) 2026 Solo Zero Labs.
