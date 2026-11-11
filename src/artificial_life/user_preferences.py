"""Reviewed preference imports through the workspace's existing writer."""
import hashlib
import json
import re

from .storage import atomic_write_json, atomic_write_text


def review_preferences(agent, entries, review=None):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError('entries must contain 1..100 preferences')
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict) or not {'text', 'sources', 'status'} <= set(entry) or set(entry) - {'text', 'sources', 'status', 'category', 'detail', 'confidence'}:
            raise ValueError('Each entry requires text, sources and status; optional category, detail and confidence')
        wording, sources, status = entry['text'], entry['sources'], entry['status']
        if not isinstance(wording, str) or wording != wording.strip() or not 1 <= len(wording) <= 200 or not wording.isprintable():
            raise ValueError('Preference text must be one printable line of 1..200 characters')
        if status not in ('inferred', 'confirmed'):
            raise ValueError('status must be inferred or confirmed')
        if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
            raise ValueError('Each preference needs 1..20 source references')
        if any(not isinstance(source, str) or not source.isprintable() or not 1 <= len(source) <= 2000 for source in sources):
            raise ValueError('Invalid source reference')
        item = {'text': wording, 'sources': list(dict.fromkeys(sources)), 'status': status}
        for key, maximum in (('category', 80), ('detail', 2000)):
            if key in entry:
                value = entry[key]
                if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(not c.isprintable() and c != '\n' for c in value):
                    raise ValueError(f'Invalid preference {key}')
                item[key] = value.strip()
        if 'confidence' in entry:
            if entry['confidence'] not in ('low', 'medium', 'high'):
                raise ValueError('confidence must be low, medium or high')
            item['confidence'] = entry['confidence']
        normalized.append(item)
    if len({entry['text'] for entry in normalized}) != len(normalized):
        raise ValueError('Duplicate preference text')
    path = agent.user_core.path
    original = path.read_bytes()
    sections = agent.user_core.read()
    current = sections['Stable Preferences']
    existing = set(current.splitlines())
    additions = []
    for entry in normalized:
        label = '已确认' if entry['status'] == 'confirmed' else '推测，待确认'
        line = f"- [{label}] {entry['text']}"
        if line not in existing and entry['text'] not in existing and f"- {entry['text']}" not in existing:
            additions.append({**entry, 'line': line})
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode('utf-8')
    if len(encoded) > 400000:
        raise ValueError('Preference import exceeds 400000 bytes')
    bundle_id = hashlib.sha256(encoded).hexdigest()
    source_ref = f'memory/preference_imports/{bundle_id}.json'
    digest = hashlib.sha256(original + str(agent.root.resolve()).encode('utf-8') + encoded).hexdigest()
    profile_ref = f'memory/preference_imports/{bundle_id}.md'
    result = {'root': str(agent.root.resolve()), 'path': str(path.resolve()), 'section': 'Stable Preferences',
              'review': digest, 'additions': additions, 'source_ref': source_ref, 'saved': False}
    result['profile_ref'] = profile_ref
    if review is None:
        return result
    # Identical retries after a successful commit are harmless.
    if not additions and (source_ref in current or not any('detail' in entry for entry in normalized)):
        return result
    if review != digest:
        raise ValueError('Memory or proposed preferences changed. Preview again.')
    bundle = agent.root / source_ref
    if not bundle.exists():
        atomic_write_json(bundle, {'schema': 'preference-import/v1', 'entries': normalized})
    elif json.loads(bundle.read_text(encoding='utf-8')) != {'schema': 'preference-import/v1', 'entries': normalized}:
        raise ValueError('Preference source bundle changed.')
    profile = agent.root / profile_ref
    rendered = render_profile(normalized, source_ref)
    if not profile.exists():
        atomic_write_text(profile, rendered)
    elif profile.read_text(encoding='utf-8') != rendered:
        raise ValueError('Preference profile changed.')
    stamp = agent.clock.now().isoformat()
    lines = [f"- [{'已确认' if entry['status'] == 'confirmed' else '推测，待确认'}] {entry['text']}" for entry in normalized]
    concise = '\n'.join(lines)
    imported_lines = set(lines)
    # Refresh identical synopsis lines while retaining earlier source citations.
    previous = '\n'.join(line for line in current.splitlines() if line not in imported_lines).strip()
    sections['Stable Preferences'] = f'{concise}\n\n[{stamp}] Sources: {source_ref}.\n\n{previous}'.rstrip()
    agent.user_core.write(sections)
    return {**result, 'saved': True}


def render_profile(entries, source_ref):
    lines = ['# 用户偏好详细画像', '', f'来源记录位于 {source_ref}。', '',
             '条目保留观察依据与推测状态。判断强弱表示线索对推测的支持程度，使用频率、当前喜好和具体动机仍需结合用户反馈。', '']
    for index, entry in enumerate(entries, 1):
        status = '已确认' if entry['status'] == 'confirmed' else '推测，待确认'
        category = entry.get('category', '未分类')
        confidence = {'high': '较强', 'medium': '中等', 'low': '较弱'}.get(entry.get('confidence'), '未评级')
        lines.extend([f'## {index}. {entry["text"]}', '', f'领域为 {category}，状态为{status}，线索支持程度为{confidence}。', ''])
        if entry.get('detail'):
            lines.extend([entry['detail'], ''])
        lines.extend(['依据来源如下。', ''])
        lines.extend(f'- {source}' for source in entry['sources'])
        lines.append('')
    return '\n'.join(lines)


def get_preferences(agent, query='', offset=0, limit=10):
    if not isinstance(query, str) or len(query) > 200 or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Expected query <=200 characters, offset >=0 and limit 1..20')
    current = agent.user_core.read()['Stable Preferences']
    bundles = list(dict.fromkeys(re.findall(r'memory/preference_imports/([0-9a-f]{64})\.json', current)))
    entries, seen = [], set()
    for identifier in bundles:
        reference = f'memory/preference_imports/{identifier}.json'
        values = read_import(agent.root, identifier)
        for index, entry in enumerate(values):
            if entry['text'] in seen:
                continue
            seen.add(entry['text'])
            searchable = '\n'.join(entry.get(key, '') for key in ('text', 'category', 'detail')).casefold()
            if query.casefold() in searchable:
                entries.append({**entry, 'source_ref': f'{reference}#entries/{index}'})
    page = entries[offset:offset+limit]
    return {'root': str(agent.root.resolve()), 'query': query, 'total': len(entries), 'offset': offset,
            'entries': page, 'next_offset': offset+len(page) if offset+len(page) < len(entries) else None}


def read_import(root, identifier):
    if not re.fullmatch(r'[0-9a-f]{64}', identifier):
        raise ValueError('Invalid preference bundle ID')
    reference = f'memory/preference_imports/{identifier}.json'
    path = root / reference
    if path.resolve() != root.resolve() / reference:
        raise ValueError('Preference bundle must remain within the selected workspace')
    document = json.loads(path.read_text(encoding='utf-8'))
    values = document['entries']
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    if document.get('schema') != 'preference-import/v1' or digest != identifier:
        raise ValueError('Preference bundle integrity check failed')
    return values
