"""Transactional outbox in the existing database. No background service."""
import json
import time
import uuid

from scripts.collector.store import canonical, digest, utc_now

SCHEMA = '''
CREATE TABLE IF NOT EXISTS events (
 event_key TEXT PRIMARY KEY, source TEXT, message_id TEXT, version INTEGER,
 event_type TEXT, state TEXT, detected_at TEXT, updated_at TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL, claim_token TEXT,
 result TEXT, error TEXT, UNIQUE(source,message_id,version));
CREATE TABLE IF NOT EXISTS event_history (
 id INTEGER PRIMARY KEY, event_key TEXT, state TEXT, at TEXT, detail TEXT);
'''


def insert_event(db, material, version, event_type):
    identity = material.get('source_identity') or {}
    key = digest(['crypto-research', material['source_key'], identity.get('web_peer_route'),
                  material['message_id'], version])
    now = utc_now()
    db.execute('INSERT INTO events(event_key,source,message_id,version,event_type,state,detected_at,updated_at) '
               'VALUES(?,?,?,?,?,?,?,?)', (key,material['source_key'],material['message_id'],version,event_type,'WAITING',now,now))
    for state in ('DETECTED','WAITING'):
        db.execute('INSERT INTO event_history(event_key,state,at,detail) VALUES(?,?,?,?)', (key,state,now,'{}'))


def list_events(store):
    with store.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM events ORDER BY detected_at,event_key')]


def recover(store, now=None):
    now = time.time() if now is None else now
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        expired = db.execute("SELECT event_key FROM events WHERE claim_token IS NOT NULL AND lease_until<?", (now,)).fetchall()
        for row in expired:
            db.execute("UPDATE events SET state='NEEDS_REVIEW',error='DELIVERY_OUTCOME_UNKNOWN',claim_token=NULL,lease_until=NULL,updated_at=? WHERE event_key=?",(utc_now(),row[0]))
            db.execute('INSERT INTO event_history(event_key,state,at,detail) VALUES(?,?,?,?)',
                       (row[0],'NEEDS_REVIEW',utc_now(),'{"reason":"DELIVERY_OUTCOME_UNKNOWN"}'))
    return len(expired)


def claim(store, event_key=None, live_budget=False):
    recover(store)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if live_budget:
            reserved=db.execute("SELECT COUNT(DISTINCT event_key) FROM event_history "
                                "WHERE state IN ('LIVE_RESERVED','DELIVERED') "
                                "AND substr(at,1,10)=date('now')").fetchone()[0]
            if reserved>=1:return None
        sql = "SELECT * FROM events WHERE state IN ('WAITING','RETRY') AND claim_token IS NULL AND attempts<3"
        params=[]
        if event_key:
            sql+=' AND event_key=?';params.append(event_key)
        row=db.execute(sql+' ORDER BY detected_at,event_key LIMIT 1',params).fetchone()
        if not row:return None
        token=uuid.uuid4().hex
        db.execute('UPDATE events SET claim_token=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE event_key=?',
                   (token,time.time()+300,utc_now(),row['event_key']))
        if live_budget:
            db.execute('INSERT INTO event_history(event_key,state,at,detail) VALUES(?,?,?,?)',
                       (row['event_key'],'LIVE_RESERVED',utc_now(),'{}'))
        return dict(row,claim_token=token)


def transition(store, event, state, result=None, error=None):
    allowed={'DELIVERED','ACCEPTED','COMPLETED','RETRY','NEEDS_REVIEW'}
    if state not in allowed:raise ValueError('Invalid event state')
    terminal=state in {'COMPLETED','RETRY','NEEDS_REVIEW'}
    with store.connect() as db:
        changed=db.execute('UPDATE events SET state=?,result=?,error=?,updated_at=?, '
            'claim_token=CASE WHEN ? THEN NULL ELSE claim_token END, lease_until=CASE WHEN ? THEN NULL ELSE lease_until END '
            'WHERE event_key=? AND claim_token=?',
            (state,canonical(result) if result is not None else None,error,utc_now(),terminal,terminal,event['event_key'],event['claim_token'])).rowcount
        if changed!=1:raise RuntimeError('EVENT_CLAIM_LOST')
        db.execute('INSERT INTO event_history(event_key,state,at,detail) VALUES(?,?,?,?)',
                   (event['event_key'],state,utc_now(),canonical({'error':error})))
