"""Preference imports reach both model contexts through the sole lifecycle writer."""
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from artificial_life.plugin import PluginService, load_agent
from artificial_life.exploration import assess_intent
from artificial_life.webui import create_server, WebUIConfig
from artificial_life.lease import RootLease


def test_preference_import_context_reload_retry_and_isolation(tmp_path):
    service = PluginService(load_agent(tmp_path / 'selected-persona', harness=True))
    other = PluginService(load_agent(tmp_path / 'other-persona', harness=True))
    service.call('exploration_mode', {'mode': 'user_service', 'conversation_id': 'owner'})
    before = service.agent.user_core.path.read_bytes()
    schedule = service.agent.runtime.schedule.all()
    timeline = service.agent.runtime.timeline.events()
    entries = [{'text': '用户可能喜欢《鸣潮》。', 'sources': ['software-example 鸣潮安装记录'], 'status': 'inferred'},
               {'text': '用户对音乐制作有兴趣。', 'sources': ['用户直接确认'], 'status': 'confirmed'}]
    preview = service.call('user_preferences', {'entries': entries})
    assert service.agent.user_core.path.read_bytes() == before
    committed = service.call('user_preferences', {'entries': entries, 'review': preview['review']})
    assert committed['saved']
    assert not service.call('user_preferences', {'entries': entries, 'review': preview['review']})['saved']
    saved = service.agent.user_core.read()['Stable Preferences']
    assert saved.startswith('- [推测，待确认] 用户可能喜欢《鸣潮》。\n- [已确认] 用户对音乐制作有兴趣。')
    assert json.loads((service.agent.root / committed['source_ref']).read_text(encoding='utf-8'))['entries'] == entries
    reloaded = PluginService(load_agent(service.agent.root, harness=True))
    autonomous = reloaded.call('context_get', {'kind': 'self'})['base']['user_service']
    dialogue = reloaded.call('context_get', {'kind': 'user', 'conversation_id': 'owner'})['base']
    ref = 'memory/user_core_memory.md#Stable Preferences'
    for value in (autonomous, dialogue):
        assert '鸣潮' in value['user_core_memory']['Stable Preferences']
    assert ref in autonomous['source_refs']
    assert ref in dialogue['user_core_source_refs']
    assert assess_intent(service.agent.raw, 'owner', {'source_refs': [ref]})['verified_source_refs'] == [ref]
    assert not other.agent.user_core.read()['Stable Preferences']
    assert service.agent.runtime.schedule.all() == schedule
    assert service.agent.runtime.timeline.events() == timeline


def test_stale_preview_and_invalid_entries(tmp_path):
    service = PluginService(load_agent(tmp_path, harness=True))
    entry = {'text': '测试偏好', 'sources': ['用户原话'], 'status': 'confirmed'}
    preview = service.call('user_preferences', {'entries': [entry]})
    service.agent.merge_user_core('Important Facts', 'Concurrent update')
    before = service.agent.user_core.path.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        service.call('user_preferences', {'entries': [entry], 'review': preview['review']})
    assert service.agent.user_core.path.read_bytes() == before
    for invalid in ({**entry, 'sources': []}, {**entry, 'status': 'certain'}, {**entry, 'text': 'heading\n## injected'}):
        with pytest.raises(ValueError):
            service.call('user_preferences', {'entries': [invalid]})


def test_cli_import_over_actual_http_owner(tmp_path):
    service = PluginService(load_agent(tmp_path, harness=True))
    server = create_server(service, WebUIConfig(port=0, auto_heartbeat=False))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    script = Path(__file__).resolve().parents[1] / 'plugins/desktop-preferences/preferences.py'
    payload = json.dumps([{'text': '用户可能喜欢音乐制作。', 'sources': []}])
    command = [sys.executable, '-X', 'utf8', str(script)]
    target = ['--connect', f'http://127.0.0.1:{server.server_address[1]}']
    try:
        preview = subprocess.run([*command, 'preview', *target], input=payload, capture_output=True, encoding='utf-8')
        assert preview.returncode == 0, preview.stderr
        review = json.loads(preview.stdout)['review']
        saved = subprocess.run([*command, 'save', *target, '--review', review], input=payload, capture_output=True, encoding='utf-8')
        assert saved.returncode == 0, saved.stderr
        assert json.loads(saved.stdout)['saved']
        assert '[推测，待确认]' in service.agent.user_core.read()['Stable Preferences']
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_offline_cli_respects_lifecycle_writer_lease(tmp_path):
    service = PluginService(load_agent(tmp_path, harness=True))
    script = Path(__file__).resolve().parents[1] / 'plugins/desktop-preferences/preferences.py'
    payload = json.dumps([{'text': '用户可能喜欢音乐制作。', 'sources': []}])
    command = [sys.executable, '-X', 'utf8', str(script)]
    target = ['--workspace', str(tmp_path)]
    preview = subprocess.run([*command, 'preview', *target], input=payload, capture_output=True, encoding='utf-8')
    assert preview.returncode == 0, preview.stderr
    review = json.loads(preview.stdout)['review']
    before = service.agent.user_core.path.read_bytes()
    with RootLease(tmp_path):
        saved = subprocess.run([*command, 'save', *target, '--review', review], input=payload, capture_output=True, encoding='utf-8')
    assert saved.returncode != 0
    assert '--connect' in saved.stderr
    assert service.agent.user_core.path.read_bytes() == before


def test_detailed_preferences_paging_provenance_and_old_format(tmp_path):
    service = PluginService(load_agent(tmp_path, harness=True))
    basic = [{'text': '音乐制作', 'sources': ['软件安装记录'], 'status': 'inferred'}]
    preview = service.call('user_preferences', {'entries': basic})
    service.call('user_preferences', {'entries': basic, 'review': preview['review']})
    # Upgrade an existing short entry with details, even if no new core line is needed.
    enriched = [{**basic[0], 'category': '音乐', 'detail': '依据多种音源与工作站推测；具体风格待确认。', 'confidence': 'medium'}]
    preview = service.call('user_preferences', {'entries': enriched})
    result = service.call('user_preferences', {'entries': enriched, 'review': preview['review']})
    assert result['saved']
    assert not service.call('user_preferences', {'entries': enriched, 'review': preview['review']})['saved']
    page = service.call('user_preferences_get', {'query': '音乐', 'limit': 1})
    assert page['total'] == 1
    assert page['entries'][0]['detail'] == enriched[0]['detail']
    assert page['entries'][0]['source_ref'].endswith('#entries/0')
    assert '依据多种音源' in (service.agent.root / result['profile_ref']).read_text(encoding='utf-8')
    entries = [{'text': f'兴趣条目{index}', 'sources': [f'证据{index}'], 'status': 'inferred',
                'category': '测试领域', 'detail': '详细判断和待确认点。', 'confidence': 'low'} for index in range(45)]
    preview = service.call('user_preferences', {'entries': entries})
    result = service.call('user_preferences', {'entries': entries, 'review': preview['review']})
    first = service.call('user_preferences_get', {'query': '测试领域', 'limit': 20})
    second = service.call('user_preferences_get', {'query': '测试领域', 'offset': first['next_offset'], 'limit': 20})
    third = service.call('user_preferences_get', {'query': '测试领域', 'offset': second['next_offset'], 'limit': 20})
    assert first['total'] == 45
    granular_ref = first['entries'][0]['source_ref']
    assessment = assess_intent(service.agent.raw, 'default', {'source_refs': [granular_ref, granular_ref.rsplit('/', 1)[0] + '/9999']})
    assert assessment['verified_source_refs'] == [granular_ref]
    assert len(first['entries']) + len(second['entries']) + len(third['entries']) == 45
    assert third['next_offset'] is None
    other = PluginService(load_agent(tmp_path / 'another-persona', harness=True))
    assert other.call('user_preferences_get')['total'] == 0
    path = service.agent.root / result['source_ref']
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['entries'][0]['detail'] = 'Changed historical source'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='integrity'):
        service.call('user_preferences_get')
