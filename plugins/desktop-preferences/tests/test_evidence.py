"""Regression checks for source attribution and cross-device import paths."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import evidence
import preferences


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_chat_defaults_to_others_and_labels_own_text_when_enabled(self):
        path = self.root / 'chat.json'
        path.write_text(json.dumps({'schema': 'preference-chat/v1', 'messages': [
            {'sender_id': 'friend', 'text': 'I love this game'},
            {'sender_id': 'me', 'text': 'I like Wuthering Waves'},
            {'sender_id': 'me', 'text': 'quoted interest', 'quoted': True},
            {'sender_id': 'me', 'text': 'forwarded interest', 'forwarded': True},
            {'sender_id': 'me', 'text': 'voice caption', 'type': 'voice'},
        ]}), encoding='utf-8')
        items = evidence.import_chat(path, 'me', 100)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['source'], 'messages[0]')
        self.assertEqual(items[0]['kind'], 'conversation-context')
        deep = evidence.import_chat(path, 'me', 20, deep_personal_reading=True)
        self.assertEqual(len(deep), 2)
        self.assertEqual(deep[1]['excerpt'], 'I like Wuthering Waves')
        self.assertEqual(deep[1]['speaker'], 'self')

    def test_all_history_schemas_strip_url_credentials_and_query(self):
        for kind, schema, insert in [
            ('chromium', 'CREATE TABLE urls(id, url, title, last_visit_time)', 'INSERT INTO urls VALUES(1, ?, ?, 100)'),
            ('firefox', 'CREATE TABLE moz_places(id, url, title, last_visit_date)', 'INSERT INTO moz_places VALUES(1, ?, ?, 100)'),
            ('safari', 'CREATE TABLE history_items(id, url); CREATE TABLE history_visits(history_item, title, visit_time)', None),
        ]:
            with self.subTest(kind=kind):
                path = self.root / f'{kind}.db'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript(schema)
                    url = 'https://person:secret@example.org/private?q=token#secret'
                    if insert:
                        db.execute(insert, (url, 'Game guide'))
                    else:
                        db.execute('INSERT INTO history_items VALUES(1, ?)', (url,))
                        db.execute("INSERT INTO history_visits VALUES(1, 'Game guide', 100)")
                    db.commit()
                before = path.read_bytes()
                items = evidence.import_history(path, 10)
                self.assertEqual(items[0]['host'], 'example.org')
                self.assertNotIn('secret', json.dumps(items))
                self.assertEqual(path.read_bytes(), before)

    def test_android_uses_exact_selected_device(self):
        result = subprocess.CompletedProcess([], 0, stdout='package:com.example.game\n', stderr='')
        with patch.object(evidence.subprocess, 'run', return_value=result) as run:
            items = evidence.android_apps('adb', 'selected-phone', 100)
        self.assertEqual(run.call_args.args[0], ['adb', '-s', 'selected-phone', 'shell', 'pm', 'list', 'packages', '-3'])
        self.assertEqual(items[0]['name'], 'com.example.game')

    def test_ios_export_to_existing_memory_and_stale_report(self):
        apps = self.root / 'apps.json'
        apps.write_text('["鸣潮"]', encoding='utf-8')
        report = self.root / 'report.json'
        def cli(*args, payload=None):
            return subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'preferences.py'), *args],
                                  input=payload, capture_output=True, encoding='utf-8')
        result = cli('observe', 'apps', '--platform', 'ios', '--input', str(apps), '--output', str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        observations = json.loads(report.read_text(encoding='utf-8'))['observations']
        user_memory = self.root / 'workspace/memory/user_core_memory.md'
        user_memory.parent.mkdir(parents=True)
        user_memory.write_text('# User Core Memory\n<!-- core_memory schema_version:1 -->\n\n## Stable Preferences\nExisting\n\n## Communication Habits\nKeep\n', encoding='utf-8')
        original = user_memory.read_bytes()
        payload = json.dumps([{'text': '用户喜欢《鸣潮》。', 'sources': [observations[0]['id']]}])
        common = ['--workspace', str(user_memory.parent.parent), '--evidence', str(report)]
        preview = cli('preview', *common, payload=payload)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        review = json.loads(preview.stdout)['review']
        self.assertEqual(user_memory.read_bytes(), original)
        report.write_text(report.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        self.assertNotEqual(cli('save', *common, '--review', review, payload=payload).returncode, 0)
        preview = cli('preview', *common, payload=payload)
        review = json.loads(preview.stdout)['review']
        saved = cli('save', *common, '--review', review, payload=payload)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        memory = user_memory.read_text(encoding='utf-8')
        self.assertIn('用户喜欢《鸣潮》。', memory)
        self.assertIn('apps[0]', memory)
        self.assertIn('Existing', memory)
        self.assertIn('## Communication Habits\nKeep', memory)
        self.assertNotEqual(cli('observe', 'apps', '--platform', 'ios', '--input', str(apps), '--output', str(report)).returncode, 0)

class RealFormatRegressionTests(unittest.TestCase):
    def test_qq_exporter_uses_owner_identity_and_bounds_excerpts(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'qq-export.json'
            messages = [{'sender': {'uid': 'other', 'uin': '200'}, 'type': 'text', 'content': {'text': 'Other person likes games'}}]
            messages += [{'sender': {'uid': 'owner-uid', 'uin': '100'}, 'type': 'text', 'content': {'text': f'{index} ' + 'x'*300}, 'timestamp': index} for index in range(30)]
            messages += [{'sender': {'uid': 'owner-uid'}, 'type': 'reply', 'content': {'text': 'Quoted reply'}}]
            path.write_text(json.dumps({'metadata': {'name': 'QQChatExporter', 'version': '0.1.0'},
                'chatInfo': {'selfUid': 'owner-uid', 'selfUin': '100'}, 'messages': messages}), encoding='utf-8')
            items = evidence.import_chat(path, None, 20, 120, True)
            self.assertEqual(len(items), 20)
            self.assertTrue(all(len(item['excerpt']) <= 120 for item in items))
            self.assertEqual(items[0]['source'], 'messages[11]')
            self.assertFalse(any('Other person' in item['excerpt'] or 'Quoted reply' in item['excerpt'] for item in items))
            shallow = evidence.import_chat(path, None, 20)
            self.assertEqual(len(shallow), 1)
            self.assertEqual(shallow[0]['kind'], 'conversation-context')

    def test_history_snapshot_keeps_original_database_and_journal(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'History'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE urls(id, url, title, last_visit_time)')
                db.execute("INSERT INTO urls VALUES(1, 'https://example.org/', 'Page', 100)")
                db.commit()
            journal = Path(str(path) + '-journal')
            journal.write_bytes(bytes(8720))
            original = path.read_bytes()
            journal_original = journal.read_bytes()
            items = evidence.import_history_snapshot(path, 20)
            self.assertEqual(items[0]['host'], 'example.org')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(journal.read_bytes(), journal_original)


if __name__ == '__main__':
    unittest.main()
