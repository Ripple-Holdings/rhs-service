# RHS repairs service

The shared record for the RHS repairs book, and the thing that actually sends
the texts and emails.

Runs as its **own Railway service with its own database**, inside your existing
project. It does not import from the Ripple Pay platform, does not share its
tables, and does not touch main.py. Neither can break the other, and a rollback
on one cannot remove part of the other.

## What it does

| | |
|---|---|
| `GET /health` | is it alive, no key needed |
| `GET /book` | the whole book, for the till to load |
| `POST /jobs` | the till pushing up whatever changed |
| `POST /send` | send one text or email |
| `POST /sms/inbound` | where Twilio posts a customer's reply |
| `GET /replies` | replies the till has not collected yet |

The till holds its own copy and works with no signal, so this is the shared
record and the way messages get out, not something the counter waits on.

## Setting it up

1. In your existing Railway project, **New → GitHub Repo**, pointed at this
   repo. Railway reads the Procfile and starts it.
2. In the same project, **New → Database → PostgreSQL**. Railway sets
   `DATABASE_URL` on the service automatically.
3. Set the rest of the variables (below).
4. Redeploy. The tables create themselves on boot.
5. Open `https://<your-service>.up.railway.app/health`. You want
   `{"ok": true, "jobs": 0}`.

## Variables

| Name | What it is |
|---|---|
| `DATABASE_URL` | set by Railway when you add the database |
| `RHS_API_KEY` | invent a long random string. The till sends it as `X-Shop-Key` |
| `TWILIO_ACCOUNT_SID` | starts `AC` |
| `TWILIO_AUTH_TOKEN` | |
| `TWILIO_SMS_FROM` | the UK **mobile** number, in `+447...` form |
| `SENDGRID_API_KEY` | optional. No key means email quietly does not send |
| `MAIL_FROM` | the address customer emails come from |
| `TWILIO_WEBHOOK_URL` | optional, see below |

## Turning texts on

1. Buy a **UK mobile** number in Twilio, not a local one. Local UK numbers only
   work domestically and behave badly for two-way.
2. Put it in `TWILIO_SMS_FROM` in the `+447...` format.
3. In Twilio, open that number, and under **Messaging** set *A message comes in*
   to a **Webhook**, `POST`, pointed at:

   `https://<your-service>.up.railway.app/sms/inbound`

4. Text the number from your own phone. It should appear in `GET /replies`.

If replies come back `403`, set `TWILIO_WEBHOOK_URL` to that exact webhook URL.
Twilio signs the address it called, and behind Railway's proxy the service can
see a different one, so the signature will not match.

## Things worth knowing

**Unsigned requests to the webhook are refused.** Anyone who knew the address
could otherwise post a fake reply into a customer's conversation.

**No message is ever acted on by itself.** Every reply, whatever it says,
lands in the Inbox for someone to read. Nothing is auto-accepted and nothing
is filtered out.

**There is no opt-out list.** Every message is transactional, about an item
the shop is physically holding, so there is nothing to unsubscribe from. A
customer texting STOP is simply shown in the Inbox like any other reply.

**Jobs are stored as JSON.** The till owns the shape of a job and will keep
changing it. A fixed set of columns here would have to be migrated in step with
the portal every time, and would silently drop any field it did not know about.

**Last write wins.** One shop, one counter, so two people editing the same job
in the same second is not a real scenario.

**Migrations run at boot, not on first use.** A migration hidden inside a
request handler only runs if that endpoint is hit, which is how a missing
column reaches production and surfaces on the first real customer.

## Testing it

`python test_service.py` runs the service end to end against a stand-in
database: auth, the book, sending, the Twilio signature check,
and that a reply is stored rather than acted on. 30 checks.

The SQL itself is checked separately by parsing every statement with the real
Postgres grammar, because the stand-in database does not execute it.
