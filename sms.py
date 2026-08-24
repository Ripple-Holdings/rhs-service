"""Platform-level SMS, configured the way SendGrid is.

One Twilio account for the whole platform, credentials in environment variables,
every portal calling one function. No per-client keys to collect, no per-client
setup for a merchant to get wrong.

    TWILIO_ACCOUNT_SID   starts AC...
    TWILIO_AUTH_TOKEN
    TWILIO_SMS_FROM      a Twilio number in E.164 (+447...) or a registered
                         alphanumeric sender ID such as RipplePay

Usage from anywhere:

    from sms import send_sms
    ok, err = send_sms('07624 123456', 'Your collection of 24.00 is due Friday.')

Deliberately mirrors the email helpers: absent configuration is not an error, it
just means nothing sends. A portal should never break because SMS is not set up.

This copy has NO opt-out list. Every message it sends is transactional, about
an item the shop is physically holding, so there is nothing to unsubscribe
from. Replies are read by a person in the Inbox instead.

worldpay_card._send_sms still exists and still drives booking reminders from
per-client booking_settings. That is left alone: this module is the platform
path, that one is the per-merchant path, and they do not interfere.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

TWILIO_API = 'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json'

# Twilio rejects anything longer, and a long message silently becomes several
# billable segments. Truncating is kinder than a surprise on the invoice.
MAX_BODY = 1600


def sms_config():
    """(sid, token, sender) from the environment. Any missing value means off."""
    return (
        (os.environ.get('TWILIO_ACCOUNT_SID') or '').strip(),
        (os.environ.get('TWILIO_AUTH_TOKEN') or '').strip(),
        (os.environ.get('TWILIO_SMS_FROM') or '').strip(),
    )


def sms_enabled():
    sid, token, sender = sms_config()
    return bool(sid and token and sender)


def normalise_number(raw, default_cc='44'):
    """A UK or Isle of Man number in E.164, or '' if it cannot be one.

    Isle of Man mobiles are +44 7624, so they normalise exactly like UK ones.
    Anything already carrying a + is trusted as written, which is what lets an
    international number through without special-casing every country.
    """
    n = re.sub(r'[^\d+]', '', str(raw or ''))
    if not n:
        return ''
    if n.startswith('+'):
        return n if len(n) >= 11 else ''
    if n.startswith('00'):
        n = n[2:]
        return '+' + n if len(n) >= 10 else ''
    if n.startswith('0'):
        n = default_cc + n[1:]
    elif not n.startswith(default_cc):
        # A bare national number with no country code and no leading zero.
        # Assume the default country rather than guessing wrong.
        n = default_cc + n
    return '+' + n if len(n) >= 11 else ''


# Alphanumeric sender IDs: 3 to 11 characters, letters and digits, and at least
# one letter. Twilio rejects anything else, and a digits-only value would be
# read as a (broken) phone number.
_ALPHA_SENDER = re.compile(r'^(?=.*[A-Za-z])[A-Za-z0-9]{3,11}$')


def normalise_sender(raw):
    """A usable From value, or '' if it cannot be one.

    Accepts a number in E.164 or an alphanumeric sender ID. Spaces and
    punctuation are stripped from alphanumeric IDs rather than rejected, since
    a merchant name like "Team JW" is the obvious thing to type.
    """
    s = str(raw or '').strip()
    if not s:
        return ''
    if s.startswith('+'):
        return normalise_number(s)
    cleaned = re.sub(r'[^A-Za-z0-9]', '', s)[:11]
    return cleaned if _ALPHA_SENDER.match(cleaned) else ''


def sender_for_client(client):
    """The sender a merchant's texts should come from.

    Each merchant sends under their OWN name, so a customer sees "Drewrys" not
    "RipplePay" - which matters, because an unrecognised sender on a payment
    message is the one most likely to be ignored or reported as spam.

    Falls back to the merchant name, then to the platform default, so a client
    with nothing configured still sends rather than failing.

    IMPORTANT: every alphanumeric sender ID has to be registered with Twilio for
    UK delivery. An unregistered one is filtered or silently dropped by the
    networks, so adding a merchant here is a two-step job - set the value AND
    register it. Until it is registered, leave sms_sender empty and the platform
    default is used.
    """
    if not client:
        return (os.environ.get('TWILIO_SMS_FROM') or '').strip()
    # The column is sms_sender_id in the clients table; sms_sender is accepted
    # too so a caller passing either shape works.
    explicit = normalise_sender(client.get('sms_sender_id') or client.get('sms_sender'))
    if explicit:
        return explicit
    derived = normalise_sender(client.get('name'))
    if derived:
        return derived
    return (os.environ.get('TWILIO_SMS_FROM') or '').strip()


def send_sms_for_client(client, to_number, message, timeout=10):
    """Send as the merchant rather than as the platform."""
    return send_sms(to_number, message,
                    sender=sender_for_client(client), timeout=timeout,
                    client_id=(client or {}).get('client_id'))


def send_sms(to_number, message, sender=None, timeout=10, client_id=None):
    """Send one SMS. Returns (ok, error). Never raises.

    A caller in a payment or booking path must not fail because a text did not
    go out, so every failure comes back as a value rather than an exception.
    """
    sid, token, default_sender = sms_config()
    if not (sid and token):
        return False, 'sms not configured'

    from_value = normalise_sender(sender or default_sender)
    if not from_value:
        return False, 'no valid sender configured'

    to = normalise_number(to_number)
    if not to:
        return False, 'invalid number'

    body = str(message or '').strip()
    if not body:
        return False, 'empty message'
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY - 1] + '\u2026'

    try:
        import requests
        from base64 import b64encode
        auth = b64encode(f'{sid}:{token}'.encode()).decode()
        resp = requests.post(
            TWILIO_API.format(sid=sid),
            data={'From': from_value, 'To': to, 'Body': body},
            headers={'Authorization': f'Basic {auth}'},
            timeout=timeout,
        )
        if resp.status_code in (200, 201):
            # Log the last four digits only. A full number in a log is personal
            # data sitting somewhere nobody is auditing.
            logger.info(f'SMS sent to ...{to[-4:]}')
            return True, None

        # Twilio returns a numeric code and a message worth surfacing: 21608 is
        # an unverified number on a trial account, 21211 a bad To, 21606 a From
        # the account cannot use.
        detail = ''
        try:
            j = resp.json()
            detail = f"{j.get('code', '')} {j.get('message', '')}".strip()
        except Exception:
            detail = resp.text[:200]
        logger.warning(f'Twilio {resp.status_code} sending to ...{to[-4:]}: {detail}')
        return False, detail or f'http {resp.status_code}'
    except Exception as e:
        logger.warning(f'SMS send failed: {e}')
        return False, str(e)


def send_sms_safe(to_number, message, sender=None):
    """Fire and forget. True on success, swallows everything else.

    For call sites that only want the attempt made and have nothing useful to do
    with a failure.
    """
    try:
        ok, _err = send_sms(to_number, message, sender=sender)
        return ok
    except Exception:
        return False
