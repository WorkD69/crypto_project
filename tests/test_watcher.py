"""Durability and media reuse: no Telegram or model required."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.collector.store import CollectorStore, normalize_message


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = CollectorStore(self.root/'state.sqlite3')

    def material(self, text='CRP-STAGE0 HERMES-FIRST own test'):
        return normalize_message('saved', {'message_id':'42', 'text':text, 'media':[],
            'source_identity':{'web_peer_route':'123','verified':True}})

    def test_event_is_transactional_and_one_per_version_across_restart(self):
        self.store.save(self.material())
        self.store.save(self.material())
        other = CollectorStore(self.store.path)
        events = other.events()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['event_type'],'NEW')
        self.assertEqual(events[0]['state'],'WAITING')
        other.save(self.material('CRP-STAGE0 edit'))
        self.assertEqual([e['event_type'] for e in other.events()],['NEW','EDITED'])

    def test_dry_run_does_not_deliver_or_call_hermes(self):
        from scripts.collector.handoff import dispatch
        self.store.save(self.material())
        r=dispatch(self.store,self.root,dry_run=True)
        self.assertEqual(r['hermes_calls'],0)
        self.assertEqual(self.store.events()[0]['state'],'WAITING')
        self.assertEqual(dispatch(self.store,self.root,dry_run=True)['event_key'],r['event_key'])

    def test_claim_is_exclusive_and_unknown_outcome_requires_review(self):
        self.store.save(self.material())
        e=self.store.claim_event()
        self.assertIsNone(CollectorStore(self.store.path).claim_event())
        self.store.recover_events(now=10**12)
        self.assertEqual(self.store.events()[0]['state'],'NEEDS_REVIEW')
        self.assertIsNone(self.store.claim_event())

    def test_delivery_repeated_after_completion_does_not_call_transport(self):
        from scripts.collector.handoff import dispatch
        calls=[]
        def transport(event):
            calls.append(event['event_key'])
            return {'event_key':event['event_key'],'is_test':True,'summary':'Own test',
                    'next_action':'NO_ACTION','hermes_calls':1,'qwen_calls':0}
        self.store.save(self.material())
        dispatch(self.store,self.root,dry_run=False,transport=transport)
        self.assertEqual(dispatch(self.store,self.root,dry_run=False,transport=transport)['state'],'EMPTY')
        self.assertEqual(len(calls),1)
        self.assertEqual(self.store.events()[0]['state'],'COMPLETED')

    def test_verified_local_file_reused_but_corruption_and_escape_rejected(self):
        from scripts.collector.media import reusable_media
        (self.root/'media').mkdir()
        p=self.root/'media/test.jpg';p.write_bytes(b'own')
        m={'state':'DOWNLOADED','path':'media/test.jpg','sha256':hashlib.sha256(b'own').hexdigest()}
        self.assertTrue(reusable_media(m,self.root))
        p.write_bytes(b'corrupt')
        self.assertFalse(reusable_media(m,self.root))
        self.assertFalse(reusable_media(dict(m,path='../escape.jpg'),self.root))

    def test_atomic_rollback_leaves_neither_message_nor_event(self):
        with self.store.connect() as db:
            db.executescript("CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'test rollback'); END;")
        with self.assertRaises(Exception):self.store.save(self.material())
        self.assertEqual(self.store.status()['messages'],0)
        self.assertEqual(self.store.events(),[])

    def test_changed_text_can_keep_same_media_structure(self):
        from scripts.collector.media import same_media_structure
        a={'album_id':None,'media':[{'position':0,'kind':'photo','observed_id':'50','label':None}],'text':'old'}
        self.assertTrue(same_media_structure(a,dict(a,text='new')))
        self.assertFalse(same_media_structure(a,dict(a,media=[])))

    def test_content_retrieval_does_not_expose_profile(self):
        from scripts.collector.handoff import event_payload
        self.store.save(self.material())
        payload=event_payload(self.store,self.store.events()[0])
        self.assertEqual(payload['text'],self.material()['text'])
        self.assertNotIn('profile',str(payload))

    def test_live_budget_is_reserved_atomically_at_claim(self):
        self.store.save(self.material())
        self.store.save(normalize_message('saved', {'message_id':'43',
            'text':'CRP-STAGE0 HERMES-FIRST second own test', 'media':[],
            'source_identity':{'web_peer_route':'123','verified':True}}))
        first=self.store.claim_event(live_budget=True)
        self.assertIsNotNone(first)
        self.assertIsNone(CollectorStore(self.store.path).claim_event(live_budget=True))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_history WHERE state='LIVE_RESERVED'").fetchone()[0],1)

    def test_cli_treats_confirmed_handoff_and_dry_run_as_success(self):
        from scripts.telegram_browser_collector import result_exit_code
        self.assertEqual(result_exit_code('COMPLETED'),0)
        self.assertEqual(result_exit_code('DRY_RUN'),0)
        self.assertEqual(result_exit_code('EMPTY'),0)
        self.assertEqual(result_exit_code('NEEDS_REVIEW'),2)

if __name__=='__main__':unittest.main()
