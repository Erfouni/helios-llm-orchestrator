import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agent import server
from agent.project_memory import ProjectStore


CATALOG = [{'id': 'test/model', 'context_length': 10000,
            'pricing': {'prompt': '0.000001', 'completion': '0.000002', 'request': '0'},
            'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
            'reasoning': {'supported_efforts': ['low', 'max']}, 'supported_parameters': ['reasoning', 'max_tokens']}]


class V2GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.temp.name)/'db', Path(self.temp.name)/'artifacts')
        self.patches = [mock.patch.object(server, '_project_store', self.store), mock.patch.object(server, 'LOCAL_API_KEY', ''), mock.patch.object(server, 'get_models', return_value=CATALOG), mock.patch.object(server.Handler, 'log_message')]
        for patch in self.patches: patch.start()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        p = self.store.create_project({'name':'HTTP regression','objective':'check','budget_usd':1,'token_budget':10000}, 'create')
        plan = self.store.plan_project(p['id'], {'tasks':[{'key':'a','title':'A','description':'return ok','timeout_seconds':10,'preferred_models':['test/model']}]}, 'plan', p['version'])
        self.store.transition_project(p['id'], 'start', {}, 'start', plan['project']['version'])
        self.task = plan['tasks'][0]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(2)
        for patch in reversed(self.patches): patch.stop()
        self.temp.cleanup()

    def call(self, path, data=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.httpd.server_port}'+path,
            data=None if data is None else json.dumps(data).encode(), headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=3) as r: return r.status,json.load(r)
        except urllib.error.HTTPError as r:
            return r.code,json.load(r)

    def run_task(self, data):
        return self.call('/v2/tasks/'+self.task['id']+'/run', {'version':self.task['version'], 'idempotency_key':'run', **data})

    def test_invalid_request_does_not_claim_or_call_model(self):
        with mock.patch.object(server, 'run_model') as provider:
            status, _ = self.run_task({'max_tokens':0})
        self.assertEqual(status,400)
        task = self.store.get_task(self.task['id'])
        self.assertEqual(task['status'],'ready')
        self.assertEqual(task['attempt_count'],0)
        provider.assert_not_called()

    def test_unsupported_effort_does_not_consume_attempt(self):
        with mock.patch.object(server, 'run_model', return_value={}) as provider:
            status, _ = self.run_task({'max_tokens':10, 'reasoning_effort':'high'})
        self.assertEqual(status,400)
        self.assertEqual(self.store.get_task(self.task['id'])['attempt_count'],0)
        provider.assert_not_called()

    def test_deadline_price_cap_and_reservation_are_applied(self):
        observed = {}
        def provider(data):
            observed.update(data)
            return {'model_used':'test/model','answer':'ok','usage':{'cost':0.00001,'total_tokens':20}}
        with mock.patch.object(server, 'run_model', side_effect=provider):
            status, result = self.run_task({'max_tokens':10, 'reservation_cost_usd':0, 'reservation_tokens':0})
        self.assertEqual(status,200,result)
        self.assertLessEqual(observed.get('timeout_seconds',180),10)
        self.assertEqual(observed.get('provider',{}).get('max_price',{}).get('prompt'),1.0)
        with self.store._connect() as db:
            row = db.execute('SELECT * FROM executions WHERE id=?',(result['execution_id'],)).fetchone()
        self.assertGreater(row['reservation_cost_usd'],0)
        self.assertGreater(row['reservation_tokens'],10)

    def test_health_stays_live_with_broken_store_but_readiness_fails(self):
        with mock.patch.object(server, 'project_store', side_effect=OSError('db unavailable')):
            status, body = self.call('/health')
            ready, _ = self.call('/ready')
        self.assertEqual(status,200)
        self.assertTrue(body['ok'])
        self.assertFalse(body['ready'])
        self.assertEqual(ready,503)

    def test_outbound_capacity_rejects_before_consuming_attempt(self):
        from agent.execution_runtime import ProviderRuntime
        with mock.patch.object(server, 'MAX_CONCURRENT_REQUESTS', 1):
            with ProviderRuntime(self.store.database_path, 1).call('openrouter','/chat/completions') as record:
                status, body = self.run_task({'max_tokens':10})
                record.result({'usage':{'cost':0,'total_tokens':0}})
        self.assertEqual(status,429,body)
        task = self.store.get_task(self.task['id'])
        self.assertEqual(task['status'],'ready')
        self.assertEqual(task['attempt_count'],0)

    def test_transport_retry_replays_before_catalog_lookup(self):
        with mock.patch.object(server, 'run_model', return_value={'model_used':'test/model','answer':'ok','usage':{'cost':0.00001,'total_tokens':20}}):
            first_status, first = self.run_task({'max_tokens':10})
        self.assertEqual(first_status,200,first)
        with mock.patch.object(server,'get_models',side_effect=OSError('catalog down')):
            status, body = self.run_task({'max_tokens':10})
        self.assertEqual(status,200,body)
        self.assertTrue(body['duplicate'])
        self.assertEqual(body['execution_id'],first['execution_id'])

    def test_enqueue_runs_durably_and_stops_at_verification(self):
        path='/v2/tasks/'+self.task['id']+'/enqueue'
        body={'version':self.task['version'],'idempotency_key':'queue-run','model':'test/model','max_tokens':10}
        status, job = self.call(path,body)
        self.assertEqual(status,202,job)
        self.assertEqual(self.store.get_task(self.task['id'])['attempt_count'],0)
        with mock.patch.object(server,'run_model',return_value={'model_used':'test/model','answer':'ok','usage':{'cost':0.00001,'total_tokens':20}}):
            server.task_queue().run_once()
        task=self.store.get_task(self.task['id'])
        self.assertEqual(task['status'],'verifying')
        status,result=self.call('/v2/jobs/'+job['id'])
        self.assertEqual(status,200,result)
        self.assertEqual(result['status'],'succeeded')

    def test_execution_billing_reconciliation_is_available(self):
        with mock.patch.object(server,'run_model',return_value={'model_used':'test/model','answer':'ok','usage':{}}):
            status,result=self.run_task({'max_tokens':10})
        self.assertEqual(status,200,result)
        execution=result['execution_id']
        status,response=self.call('/v2/executions/'+execution+'/reconcile',{
            'idempotency_key':'settle','cost_usd':0.00001,'tokens':20,
            'billing_evidence':{'source':'provider statement','reference':'generation-example','details':'confirmed final usage'}})
        self.assertEqual(status,200,response)
        self.assertEqual(response['task']['status'],'verifying')

    def test_benchmark_selection_checks_live_catalog_and_requirements(self):
        captured={}
        def select(category, **kwargs):
            captured.update(kwargs)
            return {'category':category, 'eligibility':{'checked': bool(kwargs.get('catalog'))}}
        with mock.patch.object(server,'select_benchmark_model',side_effect=select):
            status,response=self.call('/benchmarks/select?category=coding&requirements='+urllib.parse.quote(json.dumps({'min_context_length':2000})))
        self.assertEqual(status,200,response)
        self.assertTrue(response['eligibility']['checked'])
        self.assertEqual(captured['requirements']['min_context_length'],2000)

    def test_enqueue_does_not_silently_drop_manus_provider_intent(self):
        status,body=self.call('/v2/tasks/'+self.task['id']+'/enqueue',{
            'version':self.task['version'],'idempotency_key':'manus-request','provider':'manus','max_tokens':10})
        self.assertEqual(status,400,body)
        self.assertEqual(body['code'],'unsupported_worker')

    def test_preflight_catalog_failure_does_not_strand_queue(self):
        path='/v2/tasks/'+self.task['id']+'/enqueue'
        data={'version':self.task['version'],'idempotency_key':'preflight','model':'test/model','max_tokens':10}
        status, job=self.call(path,data)
        self.assertEqual(status,202,job)
        with mock.patch.object(server,'get_models',side_effect=OSError('catalog temporarily unavailable')):
            server.task_queue().run_once()
        task=self.store.get_task(self.task['id'])
        self.assertEqual(task['attempt_count'],0)
        data['idempotency_key']='preflight-again'
        status, newjob=self.call(path,data)
        self.assertEqual(status,202,newjob)

    def test_project_sampling_parameters_reach_provider_unchanged(self):
        observed={}
        def provider(method, path, payload, **kwargs):
            observed.update(payload)
            return {'model':'test/model','choices':[{'message':{'content':'ok'}}],
                    'usage':{'cost':0.00001,'total_tokens':20}}
        catalog=[{**CATALOG[0],'supported_parameters':['max_tokens','temperature','top_p','reasoning']}]
        with mock.patch.object(server,'get_models',return_value=catalog), mock.patch.object(server,'openrouter_request',side_effect=provider):
            status,body=self.run_task({'max_tokens':10,'temperature':0.2,'top_p':0.8,'reasoning_effort':'max'})
        self.assertEqual(status,200,body)
        self.assertEqual(observed.get('temperature'),0.2)
        self.assertEqual(observed.get('top_p'),0.8)
        self.assertEqual(observed.get('max_tokens'),10)
        self.assertEqual(observed.get('reasoning'),{'effort':'max'})

    def test_unsupported_execution_parameter_refused_before_claim(self):
        with mock.patch.object(server,'run_model') as provider:
            status,body=self.run_task({'max_tokens':10,'seed':42})
        self.assertEqual(status,400,body)
        self.assertEqual(self.store.get_task(self.task['id'])['attempt_count'],0)
        provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
