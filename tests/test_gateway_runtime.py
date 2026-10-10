import importlib.util
import json
import tempfile
import threading
import time
import os
import signal
import subprocess
import sys
import socket
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agent import server


class GatewayDeadlineTests(unittest.TestCase):
    def test_unknown_execution_settings_are_rejected_before_provider_call(self):
        for name, value in [('seed', 42), ('frequency_penalty', 0.4), ('reasoning', {'enabled': False})]:
            with self.subTest(name=name), mock.patch.object(server, 'openrouter_request') as provider:
                with self.assertRaises(server.GatewayError):
                    server.run_model({'model': 'test/model', 'prompt': 'hello', name: value})
                provider.assert_not_called()

    def test_run_model_forwards_task_deadline(self):
        observed = {}
        def provider(method, path, payload=None, timeout=180):
            observed['timeout'] = timeout
            return {'choices': [{'message': {'content': 'ok'}}], 'usage': {'cost': 0}}
        with mock.patch.object(server, 'openrouter_request', side_effect=provider):
            result = server.run_model({'model': 'test/model', 'prompt': 'hello', 'max_tokens': 10, 'timeout_seconds': 10})
        self.assertEqual(result['answer'], 'ok')
        self.assertLessEqual(observed['timeout'], 10)

    def test_max_effort_is_preserved(self):
        sent = {}
        def provider(method, path, payload=None, **kwargs):
            sent.update(payload)
            return {'choices': [{'message': {'content': 'ok'}}]}
        try:
            with mock.patch.object(server, 'openrouter_request', side_effect=provider):
                server.run_model({'model': 'test/model', 'prompt': 'hello', 'reasoning_effort': 'max'})
        except server.GatewayError as exc:
            self.fail(str(exc))
        self.assertEqual(sent['reasoning']['effort'], 'max')

    def test_benchmark_io_failure_does_not_break_liveness(self):
        with mock.patch.object(server, 'registry_status', side_effect=PermissionError('unreadable')):
            try:
                status = server.safe_benchmark_status()
            except OSError:
                self.fail('component I/O failure escaped health isolation')
        self.assertEqual(status['status'], 'error')


class ProviderRuntimeTests(unittest.TestCase):
    def runtime_module(self):
        path = Path(__file__).resolve().parents[1] / 'agent' / 'execution_runtime.py'
        self.assertTrue(path.exists(), 'persistent outbound runtime is missing')
        from agent import execution_runtime
        return execution_runtime

    def test_two_instances_share_outbound_capacity_and_record_unknown_cost(self):
        runtime = self.runtime_module()
        with tempfile.TemporaryDirectory() as root:
            a = runtime.ProviderRuntime(Path(root) / 'state.db', max_concurrency=1)
            b = runtime.ProviderRuntime(Path(root) / 'state.db', max_concurrency=1)
            with a.call('openrouter', '/chat/completions', model='test/model') as record:
                with self.assertRaises(runtime.CapacityError):
                    with b.call('openrouter', '/chat/completions'):
                        self.fail('second paid call entered')
                record.result({'usage': {}})
            rows = b.usage()['calls']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['billing_status'], 'unknown')
            self.assertIsNone(rows[0]['cost_usd'])
            with b.call('openrouter', '/chat/completions') as record:
                record.result({'usage': {'cost': 0.02, 'total_tokens': 5}})
            self.assertEqual(a.usage()['known_cost_usd'], 0.02)

    def test_provider_error_is_persisted_without_request_content(self):
        runtime = self.runtime_module()
        with tempfile.TemporaryDirectory() as root:
            service = runtime.ProviderRuntime(Path(root) / 'state.db', max_concurrency=1)
            with self.assertRaises(TimeoutError):
                with service.call('manus', 'task.create', model='manus-1.6'):
                    raise TimeoutError('PRIVATE PROMPT must never be logged')
            rows = service.usage()['calls']
            self.assertEqual(rows[0]['status'], 'failed')
            self.assertEqual(rows[0]['billing_status'], 'unknown')
            self.assertNotIn('PRIVATE PROMPT', json.dumps(rows))

    def test_quote_rejects_unknown_price_and_counts_input_bytes(self):
        runtime = self.runtime_module()
        model = {'id': 'test/model', 'context_length': 5000,
                 'pricing': {'prompt': '0.000001', 'completion': '0.000002', 'request': '0'},
                 'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']}}
        quote = runtime.quote_text_request(model, [{'role': 'user', 'content': 'سلام'}], 100)
        self.assertGreaterEqual(quote['reservation_tokens'], 108)
        self.assertGreater(quote['reservation_cost_usd'], 0.0002)
        model['pricing'] = {}
        with self.assertRaises(ValueError):
            runtime.quote_text_request(model, [{'role': 'user', 'content': 'x'}], 100)


class OutboundIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux parent-death regression')
    def test_transport_closes_when_gateway_parent_dies(self):
        connected=threading.Event(); disconnected=threading.Event(); sockets=[]
        listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen()
        def accept():
            client,_=listener.accept(); sockets.append(client)
            with client:
                client.recv(65536); connected.set()
                while client.recv(1024):
                    pass
            disconnected.set()
        thread=threading.Thread(target=accept,daemon=True); thread.start()
        actor=None
        try:
            with tempfile.TemporaryDirectory() as root:
                script=f'''from pathlib import Path
from agent import server
from agent.project_memory import ProjectStore
server._project_store=ProjectStore(Path({root!r})/'db',Path({root!r})/'artifacts')
server.OPENROUTER_BASE_URL='http://127.0.0.1:{listener.getsockname()[1]}'
server.api_key=lambda:'test-only'
server.openrouter_request('POST','/chat/completions',{{'model':'test/model'}},timeout=5)
'''
                actor=subprocess.Popen([sys.executable,'-c',script],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                self.assertTrue(connected.wait(3))
                actor.kill();actor.wait(2)
                self.assertTrue(disconnected.wait(1),'transport outlived its gateway parent')
        finally:
            if actor and actor.poll() is None: actor.kill();actor.wait(2)
            for client in sockets:
                try:client.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            listener.close();thread.join(2)

    def test_trickling_response_cannot_extend_total_deadline(self):
        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.send_response(200); self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b' '); self.wfile.flush(); time.sleep(0.08)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                from agent.project_memory import ProjectStore
                store = ProjectStore(Path(root)/'db', Path(root)/'artifacts')
                with mock.patch.object(server, 'OPENROUTER_BASE_URL', f'http://127.0.0.1:{httpd.server_port}'), mock.patch.object(server, 'api_key', return_value='test-only'), mock.patch.object(server, '_project_store', store):
                    started = time.monotonic()
                    with self.assertRaises(server.GatewayError) as error:
                        server.openrouter_request('POST', '/chat/completions', {'model': 'test/model'}, timeout=0.2)
                    elapsed = time.monotonic() - started
                self.assertEqual(error.exception.code, 'provider_timeout')
                self.assertLess(elapsed, 1.5)
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(2)

    def test_compare_children_share_provider_slots(self):
        class Reply(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                time.sleep(0.1)
                body = json.dumps({'choices': [{'message': {'content': 'OK'}}], 'usage': {'cost': 0.01, 'total_tokens': 1}}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Reply)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                from agent.project_memory import ProjectStore
                from agent.execution_runtime import ProviderRuntime
                store = ProjectStore(Path(root)/'db', Path(root)/'artifacts')
                with mock.patch.object(server, 'OPENROUTER_BASE_URL', f'http://127.0.0.1:{httpd.server_port}'), mock.patch.object(server, 'api_key', return_value='test-only'), mock.patch.object(server, '_project_store', store), mock.patch.object(server, 'MAX_CONCURRENT_REQUESTS', 1):
                    result = server.compare_models({'models':['test/a','test/b'], 'prompt':'hello', 'max_tokens':10})
                successes = [r for r in result['results'] if r.get('answer') == 'OK']
                self.assertEqual(len(successes), 1, 'Compare bypassed provider-level concurrency')
                ledger = ProviderRuntime(store.database_path, 1).usage()
                self.assertEqual(ledger['call_count'], 1)
                self.assertEqual(ledger['known_cost_usd'], 0.01)
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(2)


if __name__ == '__main__':
    unittest.main()
