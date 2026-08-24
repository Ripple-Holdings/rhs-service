"""Inbound SMS: the webhook Twilio posts customer replies to.

Two-way messaging needs somewhere for a reply to land. This is it.

WHY SIGNATURE VERIFICATION IS NOT OPTIONAL HERE. This endpoint has to be public
for Twilio to reach it, and downstream of it sits the quote auto-accept, which
books a job on a customer replying YES. Without verification anyone who knows
the URL could post a fake reply and accept a quote on a customer's behalf. Every
request is checked against Twilio's HMAC of the exact URL and body, and an
unverified one is refused before anything is stored.

ROUTING. A reply carries the number it was sent TO, and that is the reliable
signal: it is the merchant's own Twilio number. Routing by the CUSTOMER's number
instead would break as soon as one person is a customer of two merchants, which
on the Isle of Man is likely rather than theoretical. So a merchant who wants
replies needs their own number in clients.sms_sender_id, and a merchant who only
sends one-way keeps an alphanumeric sender there instead. One field, two modes.

Wiring, one line in main.py beside the other blueprints:

    from sms_inbound import sms_inbound_bp
    app.register_blueprint(sms_inbound_bp)

Then point the merchant's Twilio number at https://<host>/sms/inbound.
"""

import base64
import hashlib
import hmac
import logging
import os
import re

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

sms_inbound_bp = Blueprint('sms_inbound', __name__)

_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS sms_inbound (
        id           SERIAL PRIMARY KEY,
        client_id    VARCHAR(100),
        message_sid  VARCHAR(64) UNIQUE,
        from_number  VARCHAR(24),
        to_number    VARCHAR(24),
        body         TEXT,
        received_at  TIMESTAMP DEFAULT NOW(),
        handled      BOOLEAN DEFAULT FALSE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sms_inbound_client ON sms_inbound (client_id, received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sms_inbound_from ON sms_inbound (from_number)",
]


def run_sms_inbound_migrations(db_factory):
    """Takes the connection factory, so this file has nothing to import from
    the host application. That is what makes it portable between services."""
    conn = db_factory()
    cur = conn.cursor()
    for sql in _MIGRATIONS:
        cur.execute(sql)
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

def _public_url():
    """The URL Twilio signed.

    Behind Railway's proxy request.url reports http, but Twilio signed the https
    URL it actually called, so the signature would never match. TWILIO_WEBHOOK_URL
    overrides it outright if the host is ever rewritten in a way this cannot see.
    """
    override = (os.environ.get('TWILIO_WEBHOOK_URL') or '').strip()
    if override:
        return override
    url = request.url
    if request.headers.get('X-Forwarded-Proto', '').lower() == 'https':
        url = url.replace('http://', 'https://', 1)
    return url


def verify_twilio_signature():
    """True if this request really came from Twilio.

    Twilio signs the full URL with every POST parameter appended in key order,
    HMAC-SHA1 with the account auth token, base64 encoded.
    """
    token = (os.environ.get('TWILIO_AUTH_TOKEN') or '').strip()
    signature = request.headers.get('X-Twilio-Signature', '')
    if not token or not signature:
        return False
    payload = _public_url()
    for key in sorted(request.form.keys()):
        payload += key + request.form[key]
    digest = hmac.new(token.encode('utf-8'), payload.encode('utf-8'), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode('utf-8')
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------

def _norm(raw):
    try:
        from sms import normalise_number
        return normalise_number(raw)
    except Exception:
        return re.sub(r'[^\d+]', '', str(raw or ''))


_DB = None


def _exec(sql, params=()):
    """Run a statement and commit."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        cur.close()
    finally:
        conn.close()


def set_db(db_factory):
    """The host tells this module how to reach its database, once, at boot."""
    global _DB
    _DB = db_factory


def _conn():
    if _DB is None:
        raise RuntimeError('sms_inbound has no database; call set_db() at boot')
    return _DB()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def client_for_number(to_number):
    """One shop, one number, so there is nothing to route between.

    The platform version looks this up in a clients table because a reply
    could belong to any merchant. Here every reply belongs to RHS, so the
    number is only checked against the one this service sends from - which
    still rejects a reply arriving on some other number.
    """
    n = _norm(to_number)
    ours = _norm(os.environ.get('TWILIO_SMS_FROM') or '')
    if not n:
        return None
    if ours and n != ours:
        logger.warning('Inbound SMS arrived on a number this service does not send from')
        return None
    return {'client_id': 'rhs'}


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------

def _twiml(message=None):
    """Twilio expects TwiML. An empty Response means "say nothing back"."""
    body = ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            + (f'<Message>{message}</Message>' if message else '')
            + '</Response>')
    return Response(body, mimetype='text/xml')


@sms_inbound_bp.route('/sms/inbound', methods=['POST'])
def sms_inbound():
    if not verify_twilio_signature():
        # Deliberately terse. An attacker probing this should learn nothing, and
        # a genuine Twilio request always carries a valid signature.
        logger.warning('Rejected unsigned request to /sms/inbound')
        return jsonify({'error': 'Forbidden'}), 403

    from_number = _norm(request.form.get('From'))
    to_number = _norm(request.form.get('To'))
    body = (request.form.get('Body') or '').strip()
    sid = (request.form.get('MessageSid') or '').strip()

    client = client_for_number(to_number)
    client_id = (client or {}).get('client_id')

    # Every reply is stored and shown in the Inbox, whatever it says. These
    # are transactional messages about an item the shop is holding, so there
    # is no opt-out list and nothing is filtered out or answered on its own.
    _store(sid, client_id, from_number, to_number, body)
    if not client_id:
        logger.warning(f'Inbound SMS to {to_number} matched no client')
    return _twiml()


def _store(sid, client_id, from_number, to_number, body, handled=False):
    """Record the message. Idempotent on MessageSid, because Twilio retries."""
    try:
        _exec(
            "INSERT INTO sms_inbound (message_sid, client_id, from_number, to_number, body, handled) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (message_sid) DO NOTHING",
            (sid or None, client_id, from_number, to_number, body, handled))
    except Exception as e:
        logger.error(f'Could not store inbound SMS {sid}: {e}')
