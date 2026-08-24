"""Run the service for real, against a stand-in database.

There is no Postgres in this container, so the storage is faked. Everything
above it is genuine: the Flask app, the routing, the auth, the JSON shapes,
the Twilio signature check and the opt-out path. What this proves is the
behaviour; what it cannot prove is the SQL actually running, which is why
every statement is separately parsed with the real Postgres grammar.
"""
import base64, hashlib, hmac, json, os, sys, types

os.environ.setdefault('DATABASE_URL', 'postgres://fake')
os.environ.setdefault('RHS_API_KEY', 'test-shop-key')
os.environ.setdefault('TWILIO_ACCOUNT_SID', 'ACtest')
os.environ.setdefault('TWILIO_AUTH_TOKEN', 'tok-secret')
os.environ.setdefault('TWILIO_SMS_FROM', '+447700900123')
os.environ.setdefault('TWILIO_WEBHOOK_URL', 'https://rhs.example.com/sms/inbound')

# ---- stand-in database -----------------------------------------------------
STORE = {'jobs': {}, 'messages': [], 'settings': {}, 'inbound': [], 'optouts': set()}
SQL_SEEN = []

class FakeCursor:
    def __init__(self, dictmode=False):
        self.dictmode = dictmode
        self.rows = []
    def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        SQL_SEEN.append(s)
        low = s.lower()
        if low.startswith('create'):
            return
        if 'insert into jobs' in low:
            ref, data, stage = params[0], params[1], params[2]
            STORE['jobs'][ref] = json.loads(data)
        elif 'insert into settings' in low:
            STORE['settings'] = json.loads(params[0])
        elif 'insert into messages' in low:
            STORE['messages'].append({
                'ref': params[0], 'channel': params[1], 'kind': params[2],
                'to_addr': params[3], 'subject': params[4], 'body': params[5],
                'ok': params[6], 'error': params[7]})
        elif 'insert into sms_inbound' in low:
            STORE['inbound'].append({'message_sid': params[0], 'client_id': params[1],
                                     'from_number': params[2], 'to_number': params[3],
                                     'body': params[4], 'handled': params[5]})
        elif 'insert into sms_optouts' in low:
            STORE['optouts'].add(params[0])
        elif 'delete from sms_optouts' in low:
            STORE['optouts'].discard(params[0])
        elif 'select count(*) from jobs' in low:
            self.rows = [(len(STORE['jobs']),)]
        elif 'select data from jobs' in low:
            self.rows = [{'data': j} for j in STORE['jobs'].values()]
        elif 'from messages' in low:
            import datetime
            self.rows = [{**m, 'direction': 'out',
                          'sent_at': datetime.datetime(2026,8,24,12,0,tzinfo=datetime.timezone.utc)}
                         for m in STORE['messages']]
        elif 'select data from settings' in low:
            self.rows = [{'data': STORE['settings']}] if STORE['settings'] else []
        elif 'from sms_optouts' in low:
            self.rows = [{'x': 1}] if params[0] in STORE['optouts'] else []
        elif 'from sms_inbound' in low:
            import datetime
            self.rows = [{**r, 'received_at': datetime.datetime(2026,8,24,12,0,tzinfo=datetime.timezone.utc)}
                         for r in STORE['inbound']]
        else:
            self.rows = []
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def fetchall(self):
        return self.rows
    def close(self): pass

class FakeConn:
    def cursor(self, cursor_factory=None):
        return FakeCursor(dictmode=cursor_factory is not None)
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass

fake_pg = types.ModuleType('psycopg2')
fake_pg.connect = lambda *a, **k: FakeConn()
extras = types.ModuleType('psycopg2.extras')
extras.RealDictCursor = object
fake_pg.extras = extras
sys.modules['psycopg2'] = fake_pg
sys.modules['psycopg2.extras'] = extras

# ---- stand-in outbound -----------------------------------------------------
SENT = []
import requests as _requests
class FakeResp:
    status_code = 201
    text = 'ok'
    def json(self): return {}
def fake_post(url, **kw):
    SENT.append({'url': url, 'data': kw.get('data'), 'json': kw.get('json')})
    return FakeResp()
_requests.post = fake_post

import app as service
service.run_migrations()
c = service.app.test_client()

R = []
def check(name, cond, detail=''):
    R.append((name, bool(cond), detail))

# ---- auth ------------------------------------------------------------------
check('no key is refused', c.get('/book').status_code == 401)
check('wrong key is refused', c.get('/book', headers={'X-Shop-Key':'nope'}).status_code == 401)
check('health needs no key', c.get('/health').status_code == 200)

H = {'X-Shop-Key': 'test-shop-key'}

# ---- the book --------------------------------------------------------------
job = {'ref':'RHS-1044','stage':'Booked in','cost':None,
       'customer':{'name':'Margaret Callow','phone':'07624 481203'},
       'item':{'description':'9ct gold curb bracelet'},
       'createdAt':'@d:2026-07-02T11:15:00.000Z','events':[{'at':'@d:2026-07-02T11:15:00.000Z','text':'Booked in'}]}
r = c.post('/jobs', json={'jobs':[job], 'settings':{'businessName':'RHS Jewellers'}}, headers=H)
check('jobs accepted', r.status_code == 200 and r.get_json()['saved'] == 1, r.get_json())
check('job stored', 'RHS-1044' in STORE['jobs'])
check('settings stored', STORE['settings'].get('businessName') == 'RHS Jewellers')

job2 = dict(job); job2['stage'] = 'In progress'
c.post('/jobs', json={'jobs':[job2]}, headers=H)
check('update overwrites, no duplicate', len(STORE['jobs']) == 1 and STORE['jobs']['RHS-1044']['stage'] == 'In progress')

r = c.get('/book', headers=H)
b = r.get_json()
check('book returns the job', len(b['jobs']) == 1 and b['jobs'][0]['ref'] == 'RHS-1044')
check('dates survive as tagged strings', b['jobs'][0]['createdAt'].startswith('@d:'), b['jobs'][0]['createdAt'])
check('book has all four keys', all(k in b for k in ('jobs','messages','inbox','settings')))

r = c.post('/jobs', json={'jobs':'not a list'}, headers=H)
check('bad payload refused', r.status_code == 400)
r = c.post('/jobs', json={'jobs':[{'no':'ref'}]}, headers=H)
check('job with no ref skipped', r.get_json()['saved'] == 0)

# ---- sending ---------------------------------------------------------------
r = c.post('/send', json={'channel':'text','to':'07624 481203','ref':'RHS-1044',
                          'kind':'ready','body':'RHS Jewellers: ready to collect.'}, headers=H)
check('text sends', r.get_json()['ok'] is True, r.get_json())
check('text hit Twilio', any('api.twilio.com' in s['url'] for s in SENT))
tw = [s for s in SENT if 'twilio' in s['url']][-1]
check('E.164 conversion', tw['data']['To'] == '+447624481203', tw['data']['To'])
check('message recorded', STORE['messages'][-1]['ref'] == 'RHS-1044')

r = c.post('/send', json={'channel':'email','to':'m@example.com','subject':'Ready','body':'Hello'}, headers=H)
check('email with no key does not crash', r.status_code == 200 and r.get_json()['ok'] is False, r.get_json())
os.environ['SENDGRID_API_KEY'] = 'SG.test'
r = c.post('/send', json={'channel':'email','to':'m@example.com','subject':'Ready','body':'Hello'}, headers=H)
check('email sends once configured', r.get_json()['ok'] is True, r.get_json())
check('email hit SendGrid', any('sendgrid' in s['url'] for s in SENT))
r = c.post('/send', json={'channel':'carrier pigeon','to':'x','body':'y'}, headers=H)
check('unknown channel refused', r.status_code == 400)

# ---- inbound ---------------------------------------------------------------
def signed(form, url='https://rhs.example.com/sms/inbound', token='tok-secret'):
    payload = url + ''.join(k + form[k] for k in sorted(form))
    d = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(d).decode()

form = {'From':'+447624481203','To':'+447700900123','Body':'Yes please, go ahead','MessageSid':'SM1'}
r = c.post('/sms/inbound', data=form, headers={'X-Twilio-Signature': signed(form)})
check('signed reply accepted', r.status_code == 200, r.status_code)
check('reply stored', any(x['message_sid']=='SM1' for x in STORE['inbound']))

r = c.post('/sms/inbound', data=form, headers={'X-Twilio-Signature':'forged'})
check('UNSIGNED REPLY REFUSED', r.status_code == 403, r.status_code)

r = c.post('/sms/inbound', data=form)
check('missing signature refused', r.status_code == 403)

# STOP and START are deliberately not handled. These are transactional
# messages about an item the shop is holding, so there is no opt-out list.
stop = {'From':'+447624481203','To':'+447700900123','Body':'STOP','MessageSid':'SM2'}
r = c.post('/sms/inbound', data=stop, headers={'X-Twilio-Signature': signed(stop)})
check('STOP is just another reply', r.status_code == 200 and b'<Message>' not in r.data, r.data[:80])
check('STOP still reaches the inbox', any(x['message_sid']=='SM2' for x in STORE['inbound']))

before = len(SENT)
r = c.post('/send', json={'channel':'text','to':'07624 481203','body':'test'}, headers=H)
check('no opt-out list blocks a send', r.get_json()['ok'] is True and len(SENT) == before + 1, r.get_json())

wrong = {'From':'+447624481203','To':'+447999999999','Body':'hello','MessageSid':'SM4'}
r = c.post('/sms/inbound', data=wrong, headers={'X-Twilio-Signature': signed(wrong)})
check('reply on a foreign number still 200s', r.status_code == 200)

# ---- replies ---------------------------------------------------------------
r = c.get('/replies', headers=H)
check('replies listed', len(r.get_json()['replies']) >= 1)
check('replies need a key', c.get('/replies').status_code == 401)

# ---- report ----------------------------------------------------------------
print()
bad = 0
for name, passed, detail in R:
    print(('  PASS  ' if passed else '  FAIL  ') + name + ('' if passed else f'   -> {detail}'))
    if not passed: bad += 1
print(f'\n{len(R)-bad}/{len(R)} passed')
sys.exit(1 if bad else 0)
