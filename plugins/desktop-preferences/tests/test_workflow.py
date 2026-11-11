"""Workflow boundary checks: source grounding, retries, active persona and writer ownership."""
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[1] / 'src'))
import workflow
from evidence import record
from artificial_life.plugin import load_agent, PluginService
from artificial_life.lease import RootLease
from artificial_life.webui import create_server, WebUIConfig


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'life'
        self.agent = load_agent(self.base, harness=True)
        self.observation = record('software', 'Example Music Tool', 'installed program')
        self.report = {'schema': 'preference-evidence/v1', 'status': 'sampled', 'deep_personal_reading': False, 'observations': [self.observation]}

    def start(self, sources=None):
        with patch('workflow.request', side_effect=URLError('offline')), patch('workflow.collect_source', return_value=self.report):
            start = workflow.begin(sources or ['software'], base=self.base, endpoint='http://127.0.0.1:1', runs_root=self.root / 'runs')
            ready = workflow.prepare(start['run'])
        return ready

    def draft(self):
        return [{'text': '用户可能关注音乐制作。', 'sources': [self.observation['id']], 'category': '音乐',
                 'detail': '安装记录提供兴趣线索，实际偏好待确认。', 'confidence': 'medium'}]

    def finish(self, run, entries):
        with patch('workflow.request', side_effect=URLError('offline')):
            return workflow.finalize(run, entries)

    def test_changed_evidence_and_invented_references_cannot_write(self):
        ready = self.start()
        before = self.agent.user_core.path.read_bytes()
        bad = self.draft()
        bad[0]['sources'] = ['evidence-not-collected']
        with self.assertRaises(ValueError):
            self.finish(ready['run'], bad)
        path = Path(ready['run']) / 'evidence/software.json'
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Evidence changed'):
            self.finish(ready['run'], self.draft())
        self.assertEqual(self.agent.user_core.path.read_bytes(), before)

    def test_invalid_model_output_can_be_corrected_without_recapturing(self):
        ready = self.start()
        invalid = self.draft()
        invalid[0]['category'] = 'x' * 100
        with self.assertRaises(ValueError):
            self.finish(ready['run'], invalid)
        result = self.finish(ready['run'], self.draft())
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.finish(ready['run'], []), result)

    def test_manager_lease_prevents_offline_second_writer(self):
        ready = self.start()
        before = self.agent.user_core.path.read_bytes()
        with RootLease(self.base):
            with self.assertRaises(RuntimeError):
                self.finish(ready['run'], self.draft())
        self.assertEqual(self.agent.user_core.path.read_bytes(), before)

    def test_partial_source_failure_is_not_reported_as_full_success(self):
        ready = self.start(['software', 'qq'])
        self.assertIn('qq', ready['failures'])
        result = self.finish(ready['run'], self.draft())
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['readback_verified'])

    def test_active_persona_switch_is_rejected(self):
        manager = self.root / 'manager'
        selected = manager / 'personas/selected'
        other = manager / 'personas/other'
        load_agent(selected, harness=True)
        load_agent(other, harness=True)
        for name, path in [('selected', selected), ('other', other)]:
            (path / 'persona.json').write_text(json.dumps({'id': name}), encoding='utf-8')
        (manager / 'config').mkdir()
        active = manager / 'config/active_persona.json'
        active.write_text('{"active":"selected"}', encoding='utf-8')
        with patch('workflow.request', side_effect=URLError('offline')), patch('workflow.collect_source', return_value=self.report):
            start = workflow.begin(['software'], base=manager, endpoint='http://127.0.0.1:1', runs_root=self.root / 'runs')
            ready = workflow.prepare(start['run'])
        before = (other / 'memory/user_core_memory.md').read_bytes()
        active.write_text('{"active":"other"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'persona changed'):
            self.finish(ready['run'], self.draft())
        self.assertEqual((other / 'memory/user_core_memory.md').read_bytes(), before)

    def test_online_service_owns_commit_and_context_reads_it(self):
        service = PluginService(self.agent)
        service.call('exploration_mode', {'mode': 'user_service'})
        server = create_server(service, WebUIConfig(port=0, auto_heartbeat=False))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            endpoint = f'http://127.0.0.1:{server.server_address[1]}'
            with patch('workflow.collect_source', return_value=self.report):
                start = workflow.begin(['software'], base=self.base, endpoint=endpoint, runs_root=self.root / 'runs')
            ready = workflow.prepare(start['run'])
            result = workflow.finalize(ready['run'], self.draft())
            self.assertTrue(result['readback_verified'])
            context = service.call('context_get', {'kind': 'self'})
            self.assertIn('用户可能关注音乐制作。', context['base']['user_service']['user_core_memory']['Stable Preferences'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
