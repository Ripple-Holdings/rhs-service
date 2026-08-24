"""RHS Jewellers repairs service.

Deliberately standalone. Its own Railway service, its own database, no import
from and no dependency on the Ripple Pay platform. The two share patterns, not
code and not tables, so neither can break the other and a rollback on one
cannot silently remove part of the other.

What it does, and nothing more:

    GET  /health              is it alive
    GET  /book                the whole book for the till to load
    POST /jobs                the till pushing changes up
    POST /send                send an email or a text
    POST /sms/inbound         Twilio posting a customer's reply here
    GET  /replies             the till collecting replies it has not seen

The till is the thing people use. It holds its own copy and works with no
signal at all, so this service is the shared record and the way messages get
out, not something the counter waits on.

Environment:

    DATABASE_URL          Postgres, set by Railway when you add one
    RHS_API_KEY           the till's key, sent as X-Shop-Key
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_SMS_FROM       the UK mobile number replies come back to
    TWILIO_WEBHOOK_URL    optional, only if the signature check needs forcing
    SENDGRID_API_KEY      optional, no key means email quietly does not send
    MAIL_FROM             the address customer emails come from
"""

import hmac
import json
import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request

from sms import send_sms
from sms_inbound import sms_inbound_bp, run_sms_inbound_migrations, set_db as sms_set_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')
API_KEY = (os.environ.get('RHS_API_KEY') or '').strip()
MAIL_FROM = (os.environ.get('MAIL_FROM') or 'repairs@rhsjewellers.com').strip()

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


# The inbound module needs a way to reach the database. Without this the
# opt-out check fails open, which means a customer who texted STOP would
# keep getting messages. Wired here, at import, so it cannot be forgotten.
sms_set_db(_db)


# The job itself is stored as JSON rather than spread across columns. The till
# owns the shape of a job and will keep changing it; a schema here would have
# to be migrated in step with the portal every time, and would silently drop
# any field it did not know about. The columns alongside are only the ones
# needed to find and order things.
_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS jobs (
        ref         VARCHAR(40) PRIMARY KEY,
        data        JSONB NOT NULL,
        stage       VARCHAR(60),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs (updated_at DESC)",

    """CREATE TABLE IF NOT EXISTS messages (
        id          SERIAL PRIMARY KEY,
        ref         VARCHAR(40),
        channel     VARCHAR(10),
        kind        VARCHAR(30),
        direction   VARCHAR(4) NOT NULL DEFAULT 'out',
        to_addr     VARCHAR(200),
        subject     TEXT,
        body        TEXT,
        ok          BOOLEAN,
        error       TEXT,
        sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_messages_ref ON messages (ref, sent_at DESC)",

    """CREATE TABLE IF NOT EXISTS settings (
        id          INTEGER PRIMARY KEY DEFAULT 1,
        data        JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
]


def run_migrations():
    """Run at BOOT, not lazily inside a route.

    A migration hidden inside a request handler only runs if that particular
    endpoint is hit, which is how a missing column reaches production and shows
    up as an error on the first real use. Running here means a deploy that
    cannot prepare its own database fails loudly instead.
    """
    conn = _db()
    cur = conn.cursor()
    for sql in _MIGRATIONS:
        cur.execute(sql)
    conn.commit()
    cur.close()
    conn.close()
    run_sms_inbound_migrations(_db)
    logger.info('Migrations complete')


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _authorised():
    """The till's key. Absent config means locked, never open.

    Written out longhand rather than as one chained expression: an inline
    conditional in the middle of an `or` chain binds across the whole thing
    and quietly changes what is checked.
    """
    if not API_KEY:
        return False
    supplied = request.headers.get('X-Shop-Key')
    if not supplied:
        supplied = request.args.get('key')
    if not supplied and request.is_json:
        supplied = (request.get_json(silent=True) or {}).get('key')
    if not supplied:
        return False
    return hmac.compare_digest(str(supplied), API_KEY)


def _deny():
    return jsonify({'error': 'unauthorised'}), 401


# ---------------------------------------------------------------------------
# The book
# ---------------------------------------------------------------------------

@app.get('/health')
def health():
    """No key needed. Railway and a browser both use this to see it is up."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM jobs')
        n = cur.fetchone()[0]
        cur.close()
        conn.close()
        return jsonify({'ok': True, 'jobs': n})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.get('/book')
def get_book():
    """Everything the till needs to rebuild its copy."""
    if not _authorised():
        return _deny()
    conn = _db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT data FROM jobs ORDER BY created_at')
    jobs = [r['data'] for r in cur.fetchall()]
    cur.execute("SELECT ref, channel, kind, direction, to_addr, subject, body, sent_at "
                "FROM messages ORDER BY sent_at")
    rows = cur.fetchall()
    cur.execute('SELECT data FROM settings WHERE id = 1')
    srow = cur.fetchone()
    cur.close()
    conn.close()

    def shape(r):
        return {
            'ref': r['ref'],
            'channel': r['channel'],
            'kind': r['kind'],
            'to': r['to_addr'],
            'subject': r['subject'] or '',
            'body': r['body'] or '',
            'at': '@d:' + r['sent_at'].astimezone(timezone.utc).isoformat(),
            'inbound': r['direction'] == 'in',
        }

    return app.response_class(
        json.dumps({
            'v': 1,
            'jobs': jobs,
            'messages': [shape(r) for r in rows if r['direction'] != 'in'],
            'inbox': [shape(r) for r in rows if r['direction'] == 'in'],
            'settings': (srow or {}).get('data') or {},
        }),
        mimetype='application/json')


@app.post('/jobs')
def put_jobs():
    """The till pushing up whatever changed.

    Last write wins on purpose. One shop, one till at the counter, so two
    people editing the same job at the same instant is not a real scenario,
    and the alternative is conflict resolution nobody would ever look at.
    """
    if not _authorised():
        return _deny()
    payload = request.get_json(silent=True) or {}
    jobs = payload.get('jobs') or []
    if not isinstance(jobs, list):
        return jsonify({'error': 'jobs must be a list'}), 400

    saved = 0
    conn = _db()
    cur = conn.cursor()
    try:
        for job in jobs:
            ref = (job or {}).get('ref')
            if not ref:
                continue
            cur.execute(
                "INSERT INTO jobs (ref, data, stage, updated_at) "
                "VALUES (%s, %s, %s, NOW()) "
                "ON CONFLICT (ref) DO UPDATE SET "
                "  data = EXCLUDED.data, stage = EXCLUDED.stage, updated_at = NOW()",
                (ref, json.dumps(job), job.get('stage')))
            saved += 1
        settings = payload.get('settings')
        if isinstance(settings, dict) and settings:
            cur.execute(
                "INSERT INTO settings (id, data, updated_at) VALUES (1, %s, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()",
                (json.dumps(settings),))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f'Saving jobs failed: {e}')
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()
    return jsonify({'ok': True, 'saved': saved})


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _send_email(to_addr, subject, body):
    """SendGrid, or quietly nothing if it is not configured.

    No key is not an error. A counter must not fail to book a job in because
    email is not set up yet.
    """
    key = (os.environ.get('SENDGRID_API_KEY') or '').strip()
    if not key:
        return False, 'email not configured'
    if not to_addr:
        return False, 'no address'
    try:
        import requests
        resp = requests.post(
            'https://api.sendgrid.com/v3/mail/send',
            headers={'Authorization': f'Bearer {key}',
                     'Content-Type': 'application/json'},
            json={
                'personalizations': [{'to': [{'email': to_addr}]}],
                'from': {'email': MAIL_FROM},
                'subject': subject or 'Your repair',
                'content': [{'type': 'text/plain', 'value': body or ''}],
            },
            timeout=10)
        if resp.status_code in (200, 201, 202):
            return True, None
        return False, f'sendgrid {resp.status_code}: {resp.text[:200]}'
    except Exception as e:
        return False, str(e)


@app.post('/send')
def send():
    """Send one message and record it.

    The till composes the wording, because the templates live with the design
    and a message the counter cannot preview is a message nobody trusts. This
    only delivers it and writes down what happened.
    """
    if not _authorised():
        return _deny()
    p = request.get_json(silent=True) or {}
    channel = (p.get('channel') or '').lower()
    to_addr = (p.get('to') or '').strip()
    body = p.get('body') or ''
    subject = p.get('subject') or ''
    ref = p.get('ref')
    kind = p.get('kind')

    if channel == 'text':
        ok, err = send_sms(to_addr, body)
    elif channel == 'email':
        ok, err = _send_email(to_addr, subject, body)
    else:
        return jsonify({'error': 'channel must be text or email'}), 400

    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (ref, channel, kind, direction, to_addr, subject, body, ok, error) "
            "VALUES (%s, %s, %s, 'out', %s, %s, %s, %s, %s)",
            (ref, 'Text' if channel == 'text' else 'Email', kind, to_addr, subject, body, ok, err))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f'Could not record message: {e}')

    return jsonify({'ok': bool(ok), 'error': err})


@app.get('/replies')
def replies():
    """Customer replies the till has not collected yet.

    The till polls this rather than being pushed to, because a till behind a
    shop router has no address anything can call.
    """
    if not _authorised():
        return _deny()
    since = request.args.get('since')
    conn = _db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if since:
        cur.execute("SELECT * FROM sms_inbound WHERE received_at > %s ORDER BY received_at", (since,))
    else:
        cur.execute("SELECT * FROM sms_inbound ORDER BY received_at DESC LIMIT 100")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({'replies': [{
        'from': r['from_number'],
        'to': r['to_number'],
        'body': r['body'],
        'at': r['received_at'].astimezone(timezone.utc).isoformat(),
        'sid': r['message_sid'],
    } for r in rows]})


app.register_blueprint(sms_inbound_bp)

# Boot-time migrations. If the database cannot be prepared the service should
# not pretend to be healthy.
try:
    if DATABASE_URL:
        run_migrations()
    else:
        logger.warning('No DATABASE_URL, running without a database')
except Exception as _e:
    logger.error(f'Migrations failed at boot: {_e}')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
