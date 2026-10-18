import importlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from scripts.collector.store import CollectorStore, normalize_message


class LessonCycleTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module('scripts.hermes_lesson_cycle')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = CollectorStore(self.root/'research.sqlite3')

    def test_unknown_tool_and_arbitrary_url_are_rejected(self):
        for call in [{'name':'terminal','args':{}},
                     {'name':'read_lesson','args':{'url':'https://other.example/'}},
                     {'name':'read_method_card','args':{'path':'../../private'}}]:
            with self.assertRaises(ValueError):
                self.m.validate_plan({'event_key':'k','action':'TOOLS','calls':[call]},'k')

    def test_event_mismatch_is_not_accepted(self):
        with self.assertRaises(ValueError):
            self.m.validate_plan({'event_key':'wrong','action':'TOOLS','calls':[]},'k')

    def test_budget_counts_unknown_attempt_and_survives_restart(self):
        self.m.reserve_call(self.store,'a',1)
        other = CollectorStore(self.store.path)
        with self.assertRaises(RuntimeError):
            self.m.reserve_call(other,'a',1)
        self.m.reserve_call(other,'a',2)
        with self.assertRaises(RuntimeError):
            self.m.reserve_call(other,'b',1)

    def test_invented_quote_is_rejected(self):
        lesson={'url':self.m.LESSON_URL,'paragraphs':[{'id':'p1','text':'Свип требует возврата.'}]}
        card={'event_key':'k','type':'METHOD_CARD','facts':{},
              'author_rules':[{'id':'r1','rule':'entry','evidence':{'paragraph_id':'p1','quote':'точный вход 100'}}],
              'research_conclusions':[], 'uncertainties':[],
              'trade_levels':{'entry':None,'sl':None,'tp':[]},'draft':'text'}
        with self.assertRaises(ValueError):
            self.m.validate_card(card,'k',lesson)

    def test_native_tool_use_in_stream_is_rejected(self):
        stream='\n'.join(json.dumps(r) for r in [
            {'type':'tool_use','name':'python'},
            {'type':'result','exit_code':0,'text':'{}'}])
        with self.assertRaises(RuntimeError):
            self.m.parse_stream(stream)

    def test_failed_cli_output_is_preserved_without_retry(self):
        import subprocess
        failed = subprocess.CompletedProcess([], 1, stdout='partial stream', stderr='exit diagnostic')
        with patch.object(self.m.subprocess, 'run', return_value=failed):
            with self.assertRaises(RuntimeError):
                self.m.model_call(self.store,self.root,'k',1,'test',self.m.cli_transport)
        self.assertEqual((self.root/'call-1.stream.jsonl').read_text(),'partial stream')
        self.assertEqual((self.root/'call-1.stderr.txt').read_text(),'exit diagnostic')
        with self.assertRaises(RuntimeError):
            self.m.reserve_call(self.store,'k',1)

    def test_completed_event_is_not_sent_again(self):
        material=normalize_message('crypto_illia_bot',{'message_id':'6724','text':'lesson menu',
            'source_identity':{'verified':True,'username':'example_lesson_bot','web_peer_route':'1000000002'}})
        self.store.save(material)
        with self.store.connect() as db:
            db.execute("UPDATE events SET state='COMPLETED'")
        calls=[]
        result=self.m.run_cycle(self.store,self.root,transport=lambda *a:calls.append(a))
        self.assertEqual(result['state'],'NO_PENDING_EVENT')
        self.assertEqual(calls,[])

    def test_flattened_lesson_keeps_paragraph_ids_and_media_separate(self):
        page={'url':self.m.LESSON_URL,'title':'test','author_name':'author',
              'content':[{'tag':'p','children':['Hello ',{'tag':'strong','children':['world']}]},
                         {'tag':'figure','children':[{'tag':'img','attrs':{'src':'/file/test.png'}}]}]}
        result=self.m.lesson_content(page)
        self.assertEqual(result['paragraphs'],[{'id':'p0','text':'Hello world'}])
        self.assertEqual(result['images'],['https://telegra.ph/file/test.png'])


if __name__=='__main__':
    unittest.main()
