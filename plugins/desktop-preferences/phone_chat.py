"""Bounded, read-only Android QQ/WeChat UI sampling over an authorized ADB connection."""
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

PACKAGES = {'qq': 'com.tencent.mobileqq', 'wechat': 'com.tencent.mm'}


def bounds(node):
    match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
    return tuple(map(int, match.groups())) if match else None


def parse_android_chat(xml, app, *, screen='auto', deep_personal_reading=False, limit=10, excerpt_chars=120, max_total_chars=1600):
    from evidence import clean
    if app not in PACKAGES or screen not in ('auto', 'conversations', 'messages'):
        raise ValueError('Select qq/wechat and conversations/messages.')
    if type(deep_personal_reading) is not bool or not 1 <= limit <= 20 or not 20 <= excerpt_chars <= 160 or not 20 <= max_total_chars <= 3200:
        raise ValueError('Invalid collection settings or budgets.')
    if len(xml.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('Android UI hierarchy exceeds 2 MiB.')
    root = ET.fromstring(xml)
    nodes = list(root.iter('node'))
    selected = [node for node in nodes if node.get('package') == PACKAGES[app]]
    if not selected:
        return {'status': 'needs_open_client', 'items': []}
    parents = {child: parent for parent in root.iter() for child in parent}
    lists = [node for node in selected if re.search(r'ListView|RecyclerView', node.get('class', '')) and
             re.search(r'conversation|session|会话|聊天列表|群聊列表', node.get('resource-id', '') + ' ' + node.get('content-desc', ''), re.I)]
    if screen == 'auto':
        if lists:
            screen = 'conversations'
        else:
            height = max((bounds(node)[3] for node in selected if bounds(node)), default=0)
            composer = any('EditText' in node.get('class', '') and bounds(node) and bounds(node)[1] > height / 2 for node in selected)
            if not composer:
                return {'status': 'unsupported_layout', 'items': []}
            screen = 'messages'
    items, seen = [], set()
    remaining = max_total_chars
    if screen == 'conversations':
        if not lists:
            return {'status': 'unsupported_layout', 'items': []}
        for container in lists:
            for row in container:
                # Only the title, never the conversation preview underneath it.
                title = next((node for node in row.iter('node') if node.get('text', '').strip()), None)
                if title is None:
                    continue
                name = clean(title.get('text'), min(excerpt_chars, remaining))
                if not name or name in seen:
                    continue
                items.append({'kind': 'conversation-topic', 'name': name, 'node': nodes.index(title)})
                seen.add(name)
                remaining -= len(name)
                if len(items) == limit or remaining <= 0:
                    return {'status': 'sampled', 'items': items}
    else:
        dimensions = [bounds(node) for node in selected if bounds(node)]
        width = max((value[2] for value in dimensions), default=0)
        avatars = [node for node in selected if re.search(r'头像$', node.get('content-desc', '')) and bounds(node)]
        for avatar in avatars:
            location = bounds(avatar)
            center = (location[0] + location[2]) / 2
            speaker = 'other' if center < width / 3 else 'self' if center > width * 2 / 3 else None
            if not speaker or (speaker == 'self' and not deep_personal_reading):
                continue
            row = parents.get(avatar)
            while row is not None and row is not root:
                descendants = list(row.iter('node'))
                if sum(node in avatars for node in descendants) > 1:
                    break
                texts = [node for node in descendants if node.get('text', '').strip() and 'TextView' in node.get('class', '')]
                if texts:
                    if not any(re.search(r'引用|转发|reply|forward', node.get('content-desc', '') + ' ' + node.get('resource-id', ''), re.I) for node in descendants):
                        text = texts[-1]
                        index = nodes.index(text)
                        excerpt = clean(text.get('text'), min(excerpt_chars, remaining))
                        if excerpt and index not in seen:
                            items.append({'kind': 'user-message' if speaker == 'self' else 'conversation-context',
                                          'name': '本人发言' if speaker == 'self' else '对话中的他人发言',
                                          'speaker': speaker, 'excerpt': excerpt, 'node': index})
                            seen.add(index)
                            remaining -= len(excerpt)
                    break
                row = parents.get(row)
            if len(items) == limit or remaining <= 0:
                break
    return {'status': 'sampled' if items else 'unsupported_layout', 'items': items}


def capture_android_chat(adb, serial, app, **options):
    from evidence import record
    if not re.fullmatch(r'[A-Za-z0-9._:-]+', serial):
        raise ValueError('Invalid Android device identifier.')
    def run(*arguments):
        return subprocess.run([adb, '-s', serial, *arguments], capture_output=True, encoding='utf-8', errors='replace', timeout=30, check=True).stdout
    try:
        state = run('get-state').strip()
    except subprocess.SubprocessError as error:
        raise ValueError('Android device is offline, missing or unauthorized. Unlock the phone and allow USB debugging.') from error
    if state != 'device':
        raise ValueError('Android device must be online and authorized.')
    temporary = '/sdcard/.codex-preference-ui-' + uuid.uuid4().hex + '.xml'
    try:
        run('shell', 'uiautomator', 'dump', temporary)
        xml = run('exec-out', 'cat', temporary)
    finally:
        # Remove only the capture file created by this call.
        try:
            run('shell', 'rm', temporary)
        except (OSError, subprocess.SubprocessError):
            pass
    parsed = parse_android_chat(xml, app, **options)
    observed_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    observations = []
    for item in parsed['items']:
        detail = {key: value for key, value in item.items() if key not in ('kind', 'name', 'node')}
        observations.append(record(item['kind'], item['name'], f'android-ui {app} node={item["node"]}', observed_at=observed_at, **detail))
    return {'schema': 'preference-evidence/v1', 'platform': 'android', 'acquisition': 'live-client-uiautomator',
            'app': app, 'screen': options.get('screen', 'conversations'), 'status': parsed['status'],
            'deep_personal_reading': options.get('deep_personal_reading', False),
            'identity_basis': 'explicit-screen-selection-and-avatar-position',
            'observed_at': observed_at, 'observations': observations,
            'coverage': 'One foreground UI snapshot only. Conversation titles and other speech are indirect interest clues.'}


def add_phone_command(commands):
    parser = commands.add_parser('phone-chat', help='Read the foreground Android QQ/WeChat UI without chat exports')
    parser.add_argument('--app', choices=tuple(PACKAGES), required=True)
    parser.add_argument('--device', required=True)
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--screen', choices=('auto', 'conversations', 'messages'), default='auto')
    parser.add_argument('--deep-personal-reading', choices=('on', 'off'))
    parser.add_argument('--settings')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--excerpt-chars', type=int, default=120)
    parser.add_argument('--max-total-chars', type=int, default=1600)
    parser.add_argument('--output', required=True)


def phone_command(args):
    import json
    from pathlib import Path
    from chat_settings import read_settings
    deep = read_settings(args.settings)['deep_personal_reading'] if args.deep_personal_reading is None else args.deep_personal_reading == 'on'
    options = {'screen': args.screen, 'deep_personal_reading': deep, 'limit': args.limit,
               'excerpt_chars': args.excerpt_chars, 'max_total_chars': args.max_total_chars}
    # Check malformed options before accessing the phone.
    parse_android_chat('<hierarchy/>', args.app, **options)
    output = Path(args.output)
    if output.exists():
        raise ValueError('Evidence output already exists. Select a new file.')
    report = capture_android_chat(args.adb, args.device, args.app, **options)
    with output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return {'path': str(output.resolve()), 'status': report['status'], 'count': len(report['observations']),
            'deep_personal_reading': deep, 'next': 'Review this evidence and use --evidence with the existing preview/save workflow.'}
