"""Bounded imports for desktop and phone preference evidence (standard library only)."""

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import shutil
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

MAX_FILE = 32 * 1024 * 1024


def clean(value, limit=500):
    return ''.join(character if character.isprintable() else ' ' for character in str(value or ''))[:limit].strip()


def record(kind, name, source, **details):
    item = {'kind': kind, 'name': clean(name), 'source': clean(source, 1000), **details}
    identity = json.dumps(item, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return {'id': 'evidence-' + hashlib.sha256(identity).hexdigest()[:24], **item}


def load_json(path):
    if path.stat().st_size > MAX_FILE:
        raise ValueError('JSON input exceeds 32 MiB. Export a smaller selection.')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def import_chat(path, user_id, limit, excerpt_chars=160, deep_personal_reading=False):
    if type(deep_personal_reading) is not bool:
        raise ValueError('deep_personal_reading must be boolean.')
    document = load_json(path)
    if not isinstance(document, dict):
        raise ValueError('Expected a supported chat export JSON object.')
    metadata = document.get('metadata')
    qce = isinstance(metadata, dict) and metadata.get('name') == 'QQChatExporter'
    if document.get('schema') != 'preference-chat/v1' and not qce:
        raise ValueError('Expected preference-chat/v1 or QQChatExporter JSON.')
    if not user_id and not qce:
        raise ValueError('Normalized chat imports require the exact --user-id.')
    info = document.get('chatInfo', {}) if qce else {}
    if not isinstance(info, dict):
        raise ValueError('QQChatExporter chatInfo must be an object.')
    owner_ids = {str(value) for value in (info.get('selfUid'), info.get('selfUin')) if value and str(value) != '0'}
    if user_id:
        owner_ids = {user_id}
    if qce and not owner_ids:
        raise ValueError('QQChatExporter has no owner identity. Provide --user-id.')
    messages = document.get('messages')
    if not isinstance(messages, list):
        raise ValueError('messages must be an array.')
    observations = []
    for index in range(len(messages)-1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict):
            raise ValueError('Each message must be an object.')
        # Exact identity only. Quoted and forwarded text is not direct user speech.
        if message.get('type', 'text') != 'text' or message.get('recalled') or message.get('system'):
            continue
        if qce:
            sender = message.get('sender')
            if not isinstance(sender, dict):
                continue
            own = bool(owner_ids.intersection(str(sender.get(key, '')) for key in ('uid', 'uin')))
            content = message.get('content', {})
            content = content.get('text') if isinstance(content, dict) else None
        else:
            if not message.get('sender_id'):
                continue
            own = message.get('sender_id') == user_id
            content = message.get('text')
        if own and not deep_personal_reading:
            continue
        if message.get('forwarded') or message.get('quoted'):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        observations.append(record('user-message' if own else 'conversation-context', '本人发言' if own else '对话中的他人发言', f'messages[{index}]',
                                   excerpt=clean(content, min(excerpt_chars, 160)), speaker='self' if own else 'other', at=clean(message.get('timestamp'), 80)))
        if len(observations) >= min(limit, 20):
            break
    return list(reversed(observations))


def import_apps(path, limit):
    apps = load_json(path)
    if not isinstance(apps, list):
        raise ValueError('Application inventory must be a JSON array of names or {name, package} objects.')
    observations = []
    for index, app in enumerate(apps[:limit]):
        if isinstance(app, str):
            name, package = app, ''
        elif isinstance(app, dict) and isinstance(app.get('name'), str):
            name, package = app['name'], clean(app.get('package'), 200)
        else:
            raise ValueError('Invalid application inventory entry.')
        observations.append(record('software', name, f'apps[{index}]', package=package))
    return observations


def import_history(path, limit):
    # Explicitly selected database or copy; SQLite read-only mode honors its WAL.
    uri = path.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as connection:
        connection.execute('PRAGMA query_only=ON')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'urls' in tables:
            query = 'SELECT id, url, title, last_visit_time FROM urls ORDER BY last_visit_time DESC LIMIT ?'
            table = 'urls'
        elif 'moz_places' in tables:
            query = 'SELECT id, url, title, last_visit_date FROM moz_places ORDER BY last_visit_date DESC LIMIT ?'
            table = 'moz_places'
        elif {'history_items', 'history_visits'} <= tables:
            query = '''SELECT i.id, i.url, v.title, v.visit_time FROM history_visits v
                       JOIN history_items i ON v.history_item=i.id ORDER BY v.visit_time DESC LIMIT ?'''
            table = 'history_items'
        else:
            raise ValueError('Supported history databases: Chromium, Firefox, Safari. Export/copy an unlocked database.')
        observations = []
        for row_id, url, title, visited in connection.execute(query, (limit,)):
            try:
                parsed = urlsplit(url)
                host = parsed.hostname
            except (ValueError, TypeError):
                continue
            if parsed.scheme not in ('http', 'https') or not host:
                continue
            observations.append(record('browsing', title or host, f'{table} id={row_id}',
                                       host=clean(host, 250), visit_time_raw=clean(visited, 80)))
        return observations


def import_history_snapshot(path, limit):
    """Read a stable private copy when the browser holds its live database."""
    files = [path, Path(str(path) + '-wal'), Path(str(path) + '-journal')]
    with TemporaryDirectory(prefix='preference-history-') as directory:
        for attempt in range(3):
            before = {source: (source.stat().st_size, source.stat().st_mtime_ns) for source in files if source.exists()}
            for source in files:
                target = Path(directory) / source.name
                if source in before:
                    shutil.copyfile(source, target)
                else:
                    target.unlink(missing_ok=True)
            after = {source: (source.stat().st_size, source.stat().st_mtime_ns) for source in files if source.exists()}
            if before == after:
                # Recover a copied rollback journal locally, without writing to the browser.
                with closing(sqlite3.connect(Path(directory) / path.name, timeout=3)) as copy:
                    check = copy.execute('PRAGMA quick_check').fetchone()
                    if check != ('ok',):
                        raise ValueError('Browser history snapshot failed integrity validation.')
                return import_history(Path(directory) / path.name, limit)
        raise ValueError('Browser history kept changing. Close it or provide an export.')


def android_apps(adb, serial, limit):
    result = subprocess.run([adb, '-s', serial, 'shell', 'pm', 'list', 'packages', '-3'],
                            capture_output=True, encoding='utf-8', errors='replace', timeout=30, check=True)
    packages = sorted({line[8:].strip() for line in result.stdout.splitlines() if line.startswith('package:')})
    return [record('android-package', package, 'ADB pm list packages -3') for package in packages[:limit]]


def load_evidence(path):
    report = load_json(path)
    if not isinstance(report, dict) or report.get('schema') != 'preference-evidence/v1':
        raise ValueError('Unsupported evidence report.')
    observations = report.get('observations')
    if not isinstance(observations, list) or len(observations) > 2000:
        raise ValueError('Invalid evidence observations.')
    evidence = {}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for item in observations:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ('id', 'kind', 'name', 'source')):
            raise ValueError('Invalid evidence entry.')
        fields = {key: value for key, value in item.items() if key != 'id'}
        expected = record(fields.pop('kind'), fields.pop('name'), fields.pop('source'), **fields)
        if expected != item:
            raise ValueError('Evidence entry changed. Import again.')
        # Persist report provenance, not the raw message, in core memory.
        evidence[item['id']] = {'name': item['name'], 'source': f"{path.resolve()} {item['source']} evidence-kind={item['kind']} report-sha256={digest}"}
    return evidence


def add_commands(commands):
    importer = commands.add_parser('observe', help='Import a selected desktop/phone export, or read Android application packages.')
    importer.add_argument('kind', choices=('history', 'chat', 'apps', 'android-apps'))
    importer.add_argument('--input', type=Path)
    importer.add_argument('--platform', choices=('desktop', 'android', 'ios'), required=True)
    importer.add_argument('--user-id')
    importer.add_argument('--deep-personal-reading', choices=('on', 'off'))
    importer.add_argument('--settings')
    importer.add_argument('--device', help='Explicit ADB device serial')
    importer.add_argument('--adb', default='adb')
    importer.add_argument('--limit', type=int, default=200)
    importer.add_argument('--excerpt-chars', type=int, default=160, help='Maximum characters per chat message (20..500)')
    importer.add_argument('--snapshot', action='store_true', help='Read browser history through a temporary stable copy')
    importer.add_argument('--output', type=Path, required=True, help='New local evidence JSON file')


def observe(args):
    if not 1 <= args.limit <= 2000:
        raise ValueError('limit must be 1–2000.')
    if not 20 <= args.excerpt_chars <= 500:
        raise ValueError('excerpt-chars must be 20..500.')
    if args.snapshot and args.kind != 'history':
        raise ValueError('--snapshot applies to history only.')
    if args.kind == 'android-apps':
        if args.platform != 'android' or not args.device:
            raise ValueError('Android collection requires --platform android and --device SERIAL.')
        observations = android_apps(args.adb, args.device, args.limit)
    else:
        if args.input is None or not args.input.is_file():
            raise ValueError('Select an existing export file with --input.')
        if args.output.resolve() == args.input.resolve():
            raise ValueError('Output must differ from input.')
        if args.kind == 'chat':
            from chat_settings import read_settings
            deep = read_settings(args.settings)['deep_personal_reading'] if args.deep_personal_reading is None else args.deep_personal_reading == 'on'
            observations = import_chat(args.input, args.user_id, args.limit, args.excerpt_chars, deep)
        elif args.kind == 'apps':
            observations = import_apps(args.input, args.limit)
        else:
            observations = import_history_snapshot(args.input, args.limit) if args.snapshot else import_history(args.input, args.limit)
    # Bind record IDs to the selected source to distinguish separate exports.
    origin = str(args.input.resolve()) if args.input else f'ADB device {args.device}'
    observations = [record(item['kind'], item['name'], f"{origin} {item['source']}",
                           **{key: value for key, value in item.items() if key not in ('id', 'kind', 'name', 'source')}) for item in observations]
    report = {'schema': 'preference-evidence/v1', 'platform': args.platform,
              'observed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'observations': observations,
              'coverage': 'Selected export only; truncated to requested limit. Browsing is a clue, not a confirmed preference.'}
    if args.kind == 'chat':
        report['deep_personal_reading'] = deep
    # Exclusive creation avoids overwriting exports or earlier evidence.
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return {'path': str(args.output.resolve()), 'count': len(observations), 'platform': args.platform,
            'next': 'Read this local report to review candidates; supply --evidence FILE to preview/save.'}
