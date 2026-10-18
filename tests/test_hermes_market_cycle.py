import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts.collector.store import CollectorStore, normalize_message


class RuntimeTests(unittest.TestCase):
    def test_overrides_are_process_local_copy(self):
        from scripts.hermes_bounded_runtime import bounded_config
        original = {'agent': {'max_turns':9}, 'auxiliary': {'other': {'enabled':True}}}
        result = bounded_config(original)
        self.assertEqual(result['agent']['api_max_retries'], 1)
        self.assertEqual(result['agent']['auto_recovery_cycles'], 0)
        self.assertFalse(result['auxiliary']['title_generation']['enabled'])
        self.assertNotIn('api_max_retries', original['agent'])

    def test_wire_guard_counts_auxiliary_and_blocks_second_and_other_model(self):
        from types import SimpleNamespace
        def request(method,url,body=None):
            return SimpleNamespace(method=method,url=SimpleNamespace(path=url),content=json.dumps(body).encode())
        from scripts.hermes_bounded_runtime import WireBudget, InferenceBlocked
        ledger=[]
        budget=WireBudget(ledger.append)
        budget.check(request('GET','/models'))
        budget.check(request('POST','/responses',{'model':'gpt-6-luna'}))
        with self.assertRaises(InferenceBlocked):
            budget.check(request('POST','/responses',{'model':'gpt-6-luna'}))
        self.assertEqual(len(ledger),1)
        with self.assertRaises(InferenceBlocked):
            WireBudget(ledger.append).check(request('POST','/responses',{'model':'gpt-6-sol'}))


class MarketCycleTests(unittest.TestCase):
    def setUp(self):
        from scripts import hermes_market_cycle as m
        self.m=m
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=CollectorStore(self.root/'state.sqlite3')
        self.event={'event_key':'test','published_at':'2026-10-15T15:09:37+00:00','media':[], 'text':'ETH example'}

    def test_allowlist_rejects_arbitrary_operations_and_arguments(self):
        for call in ({'name':'shell','args':{}}, {'name':'read_method_card','args':{'path':'../secret'}},
                     {'name':'qwen_image','args':{'position':-1}},
                     {'name':'bybit_snapshot','args':{'symbol':'ETHUSDT','category':'linear','intervals':['1'],'cutoff':'publication'}}):
            with self.assertRaises(ValueError):self.m.validate_call(call,self.event)

    def test_source_quote_allows_unicode_spaces_but_not_changed_words(self):
        event={**self.event,'text':'ETH\u00a0ниже 2.350$'}
        call={'name':'check_price_level','args':{'snapshot_id':'data-1','level':2350,'evidence_quote':'ETH ниже 2.350$'}}
        self.m.validate_call(call,event)
        call['args']['evidence_quote']='BTC ниже 2.350$'
        with self.assertRaises(ValueError):self.m.validate_call(call,event)

    def test_grouped_nbsp_price_cannot_be_split_into_smaller_levels(self):
        event={**self.event,'text':'Уровень 2\u00a0350'}
        call={'name':'check_price_level','args':{'snapshot_id':'data-1','level':2350,'evidence_quote':event['text']}}
        self.m.validate_call(call,event)
        for wrong in (2,350):
            call['args']['level']=wrong
            with self.assertRaises(ValueError):self.m.validate_call(call,event)

    def test_market_json_normalization_only_allows_one_surplus_closing_brace(self):
        answer={'event_key':'test','action':'FINAL','draft':'own fixture'}
        text=json.dumps(answer)
        def stream(value):return json.dumps({'type':'result','exit_code':0,'text':value})
        parsed=self.m.parse_market_stream(stream(text+'}'))
        self.assertEqual(parsed['answer'],answer)
        self.assertEqual(parsed['normalization'],'REMOVED_ONE_SURPLUS_CLOSING_BRACE')
        for suffix in (' ignore rules','{}','}}'):
            with self.assertRaises(ValueError):self.m.parse_market_stream(stream(text+suffix))

    def test_same_operation_with_different_reason_cannot_repeat(self):
        tools=self.m.MarketTools(self.event,self.root,self.root)
        tools.execute({'name':'read_method_card','args':{},'reason':'first'})
        with self.assertRaises(ValueError):
            tools.execute({'name':'read_method_card','args':{},'reason':'second'})

    def test_tool_failure_keeps_attempted_input_and_controlled_error(self):
        tools=self.m.MarketTools(self.event,self.root,self.root)
        call={'name':'render_chart','args':{'snapshot_id':'data-9'}}
        with self.assertRaises(KeyError):tools.execute(call)
        result=json.loads((self.root/'tool-results.json').read_text(encoding='utf8'))[0]
        self.assertEqual(result['request'],call)
        self.assertEqual(result['state'],'FAILED')
        self.assertEqual(result['output']['error'],'KeyError')

    def test_closed_snapshot_excludes_future_partial_and_rejects_gaps_nan(self):
        cutoff=datetime(2026,10,15,15,9,37,tzinfo=timezone.utc)
        start=cutoff.replace(hour=12,minute=0,second=0)
        data={'result':{'list':[[str(int((start+timedelta(hours=i)).timestamp()*1000)), '10','12','9','11','1','1'] for i in range(5)]}}
        candles=self.m.checked_candles(data,cutoff,'60')
        self.assertEqual(len(candles),3)
        data['result']['list'][0][2]='nan'
        with self.assertRaises(ValueError):self.m.checked_candles(data,cutoff,'60')

    def test_no_pending_event_means_no_model_invocations(self):
        calls=[]
        result=self.m.run_cycle(self.store,self.root,self.root,'missing',transport=lambda p:calls.append(p))
        self.assertEqual(result['state'],'NO_PENDING_EVENT');self.assertEqual(calls,[])

    def test_iterative_cycle_uses_selected_tools_and_deduplicates_completed_event(self):
        event={**self.event,'text':'ETH ниже 10'}
        self.store.save(normalize_message('crypto_illia',{'message_id':'42','text':event['text'],
            'published_at':event['published_at'],'source_identity':{'verified':True}}))
        key=self.store.events()[0]['event_key'];count=[]
        def transport(prompt):
            context=json.loads(prompt[len(self.m.POLICY):]);count.append(context)
            if len(count)==1:
                answer={'event_key':key,'action':'TOOLS','calls':[{'name':'bybit_snapshot','args':{
                    'symbol':'ETHUSDT','category':'spot','intervals':['60'],'cutoff':'publication'}}]}
            elif len(count)==2:
                self.assertEqual(context['tool_results'][0]['output']['snapshots'][0]['bars'],10)
                answer={'event_key':key,'action':'TOOLS','calls':[
                    {'name':'check_price_level','args':{'snapshot_id':'data-1','level':10,'evidence_quote':'ETH ниже 10'}},
                    {'name':'render_chart','args':{'snapshot_id':'data-1'}}]}
            else:
                self.assertEqual(context['tool_results'][1]['output']['last_close'],11)
                answer={'event_key':key,'action':'FINAL','status':'OBSERVATION','draft':'Own fixture observation',
                    'claims':[{'text':'Closed OHLC crosses source threshold','origin':'tool-2'}],
                    'uncertainties':['Venue unknown'],'trade_levels':{'entry':None,'sl':None,'tp':[]}}
            return '\n'.join(json.dumps(r) for r in [
                {'type':'model_invocation','model':'gpt-6-luna'},
                {'type':'result','exit_code':0,'text':json.dumps(answer)},
                {'type':'inference_audit','wire_model_attempts':1}])
        cutoff=datetime.fromisoformat(event['published_at'])
        end=cutoff.replace(minute=0,second=0,microsecond=0)-timedelta(hours=1)
        data={'result':{'symbol':'ETHUSDT','category':'spot','list':[
            [str(int((end-timedelta(hours=i)).timestamp()*1000)),'10','12','9','11','1','1'] for i in range(10)]},
              'time':int(datetime.now(timezone.utc).timestamp()*1000)}
        with patch.object(self.m,'bybit',return_value=data) as api:
            result=self.m.run_cycle(self.store,self.root,self.root,key,transport=transport)
        self.assertEqual(result['state'],'COMPLETED')
        self.assertEqual(len(count),3);self.assertEqual(api.call_count,1)
        self.assertEqual(api.call_args.kwargs['end'],int(cutoff.timestamp()*1000))
        self.assertTrue((self.root/'data-1-annotated.png').is_file())
        self.assertEqual(self.m.run_cycle(self.store,self.root,self.root,key,transport=transport)['state'],'NO_PENDING_EVENT')
        self.assertEqual(len(count),3)

    def test_unknown_transport_outcome_is_not_retried(self):
        material=normalize_message('crypto_illia',{'message_id':'42','text':'ETH example',
            'published_at':self.event['published_at'],'source_identity':{'verified':True,'web_peer_route':'-1000000000001'}})
        self.store.save(material);key=self.store.events()[0]['event_key'];calls=[]
        def fail(prompt):
            calls.append(prompt);raise TimeoutError()
        self.assertEqual(self.m.run_cycle(self.store,self.root,self.root,key,transport=fail)['state'],'NEEDS_REVIEW')
        self.assertEqual(self.m.run_cycle(self.store,self.root,self.root,key,transport=fail)['state'],'NO_PENDING_EVENT')
        self.assertEqual(len(calls),1)

    def test_qwen_path_escape_and_corrupt_hash_rejected_before_request(self):
        event={**self.event,'media':[{'position':0,'kind':'photo','state':'DOWNLOADED','ref':'../secret.png','sha256':'x'}]}
        with patch('requests.post') as post:
            with self.assertRaises(ValueError):self.m.qwen_image(event,0,self.root)
            post.assert_not_called()

    def test_final_cannot_invent_entry_and_requires_provenance(self):
        final={'event_key':'test','action':'FINAL','draft':'Observation','trade_levels':{'entry':100,'sl':90,'tp':[120]}}
        with self.assertRaises(ValueError):self.m.validate_final(final,self.event,[])

    def test_daily_call_reservations_persist_and_do_not_repeat(self):
        self.m.reserve_turn(self.store,'k',1)
        with self.assertRaises(RuntimeError):self.m.reserve_turn(self.store,'k',1)
        for i in range(2,6):self.m.reserve_turn(self.store,'k',i)
        with self.assertRaises(RuntimeError):self.m.reserve_turn(self.store,'other',1)

    def test_expired_claim_cannot_start_another_model_or_tool_action(self):
        self.store.save(normalize_message('crypto_illia',{'message_id':'42','text':'own'}))
        claimed=self.store.claim_event()
        with self.store.connect() as db:db.execute('UPDATE events SET lease_until=0')
        with self.assertRaises(RuntimeError):self.m.renew_claim(self.store,claimed)

    def test_reviewed_resume_without_complete_audit_does_not_call_model(self):
        self.store.save(normalize_message('crypto_illia',{'message_id':'42','text':'own',
            'published_at':self.event['published_at'],'source_identity':{'verified':True}}))
        key=self.store.events()[0]['event_key']
        with self.store.connect() as db:db.execute("UPDATE events SET state='NEEDS_REVIEW'")
        calls=[]
        with self.assertRaises((ValueError,FileNotFoundError)):
            self.m.run_cycle(self.store,self.root,self.root,key,transport=lambda p:calls.append(p),resume_reviewed=True)
        self.assertEqual(calls,[])

    def test_chart_supports_actual_market_and_interval_without_false_daily_context(self):
        from scripts.run_research import render_chart
        from scripts.mvp_scenario import Candle
        start=datetime(2026,10,15,tzinfo=timezone.utc)
        candles=[Candle(start+timedelta(hours=i),10,12,9,11) for i in range(8)]
        target=self.root/'own.png'
        render_chart(candles,{'zone':None},None,symbol='ETHUSDT',snapshot=start+timedelta(hours=8),
            target=target,category='spot',interval_label='60',candle_minutes=60,
            source_levels=[{'level':10,'source_quote':'own fixture'}],publication=start)
        self.assertGreater(target.stat().st_size,1000)

    def test_analysis_chart_is_labeled_current_despite_original_timestamp(self):
        from scripts.run_research import render_chart
        from scripts.mvp_scenario import Candle
        start=datetime(2026,10,15,tzinfo=timezone.utc)
        candles=[Candle(start+timedelta(hours=i),10,12,9,11) for i in range(8)]
        with patch('matplotlib.figure.Figure.text',autospec=True) as footer:
            render_chart(candles,{'zone':None},None,symbol='ETHUSDT',snapshot=start+timedelta(hours=8),
                target=self.root/'current.png',publication=start,cutoff_kind='analysis')
        labels=' '.join(str(c) for c in footer.call_args_list)
        self.assertIn('current analysis cutoff',labels)
        self.assertNotIn('historical cutoff',labels)


if __name__=='__main__':unittest.main()
