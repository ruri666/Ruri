"""Collect local software and setting clues and save reviewed user preferences."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
from contextlib import contextmanager
from evidence import add_commands, load_evidence, observe
from chat_settings import read_settings, settings_path, update_settings
from phone_chat import add_phone_command, phone_command
import sqlite3
import subprocess
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

SETTINGS = {
    "text-size": (r"Software\Microsoft\Accessibility", "TextScaleFactor"),
    "app-theme": (
        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        "AppsUseLightTheme",
    ),
    "transparency": (
        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        "EnableTransparency",
    ),
    "mouse-speed": (r"Control Panel\Mouse", "MouseSensitivity"),
}
SECTION = "## Stable Preferences"


def call_lifecycle(url, arguments):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("--connect must be a loopback HTTP URL")
    request = Request(url.rstrip("/") + "/api/tools/call",
                      data=json.dumps({"name": "user_preferences", "arguments": arguments}).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as error:
        raise ValueError(f"Lifecycle service rejected the preference import: {error.read().decode('utf-8', errors='replace')[:1000]}") from error
    except URLError as error:
        raise ValueError("Lifecycle service is unreachable. Start the configured service before previewing or saving.") from error


def software_observation(name, source):
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or len(name) > 200 or not name.isprintable():
        return None
    identifier = hashlib.sha256(f"{source}\n{name}".encode("utf-8")).hexdigest()[:24]
    return {"id": f"software-{identifier}", "kind": "software", "name": name, "source": source}


def scan_software():
    import winreg

    observations = {}
    warnings = []
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive, label in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            source = f"{label}\\{uninstall}"
            try:
                with winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as app:
                                name, _ = winreg.QueryValueEx(app, "DisplayName")
                            item = software_observation(name, source)
                            if item:
                                observations[item["id"]] = item
                        except OSError:
                            continue
            except FileNotFoundError:
                continue
            except OSError:
                warnings.append(f"Cannot read {source} view {view}.")

    # Resolve redirected per-user shell folders without opening shortcut targets.
    folders = {}
    shell_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, shell_key) as key:
            for name in ("Desktop", "Programs"):
                try:
                    raw, _ = winreg.QueryValueEx(key, name)
                    if isinstance(raw, str):
                        folders[f"user {name}"] = Path(os.path.expandvars(raw))
                except OSError:
                    warnings.append(f"Cannot locate user {name} shortcuts.")
    except OSError:
        warnings.append("Cannot locate user shell folders.")
    if os.environ.get("PUBLIC"):
        folders["public Desktop"] = Path(os.environ["PUBLIC"]) / "Desktop"
    if os.environ.get("PROGRAMDATA"):
        folders["common Programs"] = Path(os.environ["PROGRAMDATA"]) / "Microsoft/Windows/Start Menu/Programs"
    for label, folder in folders.items():
        def on_error(error):
            warnings.append(f"Some {label} shortcuts could not be read.")
        for directory, subfolders, filenames in os.walk(folder, followlinks=False, onerror=on_error):
            subfolders[:] = [name for name in subfolders if not (Path(directory) / name).is_junction() and not (Path(directory) / name).is_symlink()]
            for filename in filenames:
                if Path(filename).suffix.lower() not in (".lnk", ".url"):
                    continue
                relative = (Path(directory) / filename).relative_to(folder).as_posix()
                item = software_observation(Path(filename).stem, f"{label} shortcut {relative}")
                if item:
                    observations[item["id"]] = item
    return {"software": sorted(observations.values(), key=lambda item: (item["name"].casefold(), item["source"])), "warnings": sorted(set(warnings))}


def scan():
    if sys.platform != "win32":
        raise ValueError("Settings collection requires a local Windows session.")
    import winreg

    observations = []
    for source, (key_path, name) in SETTINGS.items():
        value = None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                raw, _ = winreg.QueryValueEx(key, name)
            if source == "mouse-speed" and isinstance(raw, str) and re.fullmatch(r"[0-9]{1,2}", raw):
                raw = int(raw)
            if type(raw) is int:
                if source == "text-size" and 100 <= raw <= 225:
                    value = f"{raw}%"
                elif source == "app-theme" and raw in (0, 1):
                    value = ("dark", "light")[raw]
                elif source == "transparency" and raw in (0, 1):
                    value = ("off", "on")[raw]
                elif source == "mouse-speed" and 1 <= raw <= 20:
                    value = raw
        except OSError:
            pass
        observations.append({"id": source, "setting": f"HKCU\\{key_path}::{name}", "value": value})
    return {"observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "settings": observations, **scan_software()}


def preferences_from_stdin():
    payload = sys.stdin.read(400001)
    if len(payload.encode('utf-8')) > 400000:
        raise ValueError("Preference payload is too large.")
    items = json.loads(payload)
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("Provide an array of 1–100 preferences.")
    for item in items:
        if not isinstance(item, dict) or not {"text", "sources"} <= set(item) or set(item) - {"text", "sources", "category", "detail", "confidence"}:
            raise ValueError("Each preference requires text and sources, with optional category, detail and confidence.")
        wording, sources = item["text"], item["sources"]
        if not isinstance(wording, str) or wording != wording.strip() or not 1 <= len(wording) <= 500:
            raise ValueError("Preference wording must be 1–500 characters with no surrounding whitespace.")
        if any(not character.isprintable() for character in wording):
            raise ValueError("Preference wording must fit on one printable line.")
        if not isinstance(sources, list) or any(not isinstance(s, str) or (s not in SETTINGS and not re.fullmatch(r"(?:software|evidence)-[a-f0-9]{24}", s)) for s in sources):
            raise ValueError("Use source IDs from scan or an empty list for direct user input.")
    if len({item["text"] for item in items}) != len(items):
        raise ValueError("Duplicate wording in proposal.")
    return items


def target_file(workspace):
    root = Path(workspace)
    if not root.is_absolute():
        raise ValueError("Provide an absolute workspace path.")
    root = root.resolve(strict=True)
    path = root / "memory" / "user_core_memory.md"
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("Expected an existing user core memory inside the selected workspace.")
    return path


def proposal(path, original, items, software=None, status="confirmed"):
    software = software or {}
    document = original.decode("utf-8")
    lines = document.splitlines(keepends=True)
    if not lines or lines[0].strip() != "# User Core Memory":
        raise ValueError("Unrecognized user core memory title.")
    if "<!-- core_memory schema_version:1 -->" not in [line.strip() for line in lines[:3]]:
        raise ValueError("Unsupported user core memory schema.")
    headings = [index for index, line in enumerate(lines) if line.rstrip() == SECTION]
    if len(headings) != 1:
        raise ValueError("Expected exactly one Stable Preferences section.")
    start = headings[0] + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    existing = "".join(lines[start:end])
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    additions = []
    existing_lines = set(existing.splitlines())
    for item in items:
        wording = item["text"]
        label = "已确认" if status == "confirmed" else "推测，待确认"
        marked = f"[{label}] {wording}"
        if wording in existing_lines or f"- {wording}" in existing_lines or f"- {marked}" in existing_lines:
            continue
        sources = ["user confirmation" if status == "confirmed" else "inferred from observations; not user-confirmed"]
        for source in dict.fromkeys(item["sources"]):
            if source in SETTINGS:
                sources.append(f"Windows HKCU {SETTINGS[source][0]}::{SETTINGS[source][1]}")
            elif source in software:
                evidence = software[source]
                sources.append(f"{source} {evidence['name']} ({evidence['source']})")
            else:
                raise ValueError("Software evidence is no longer available. Scan and review again.")
        additions.append((marked, ", ".join(sources)))
    reviewed = json.dumps([str(path), additions], ensure_ascii=False).encode("utf-8")
    review = hashlib.sha256(hashlib.sha256(original).digest() + reviewed).hexdigest()
    return additions, review, lines, end, newline


def write_reviewed(path, original, replacement):
    with NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with temporary.open("wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != original:
            raise ValueError("Memory changed during save. Preview again.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def workspace_writer(root):
    """Use the same OS lease as Artificial Life's RootLease in standalone mode."""
    with (root / ".writer.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("Workspace has an active writer. Use --connect to its lifecycle service.") from error
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan")
    add_commands(commands)
    add_phone_command(commands)
    from workflow import add_workflow_commands, workflow_command
    add_workflow_commands(commands)
    settings_parser = commands.add_parser('settings', help='View or change shared desktop/phone collection settings')
    settings_parser.add_argument('--deep-personal-reading', choices=('on', 'off'))
    settings_parser.add_argument('--settings')
    for command in ("preview", "save"):
        subparser = commands.add_parser(command)
        target = subparser.add_mutually_exclusive_group(required=True)
        target.add_argument("--workspace", help="Offline workspace only; prefer --connect for a running lifecycle")
        target.add_argument("--connect", help="Loopback lifecycle service URL, e.g. http://127.0.0.1:8771")
        subparser.add_argument("--status", choices=("inferred", "confirmed"), default="inferred")
        subparser.add_argument("--evidence", type=Path, action="append", default=[], help="Selected evidence reports from observe")
        if command == "save":
            subparser.add_argument("--review", required=True)
    args = parser.parse_args()
    if args.command == "scan":
        return scan()
    if args.command == "observe":
        return observe(args)
    if args.command == 'phone-chat':
        return phone_command(args)
    if args.command == 'workflow':
        return workflow_command(args)
    if args.command == 'settings':
        value = read_settings(args.settings) if args.deep_personal_reading is None else update_settings(args.deep_personal_reading == 'on', args.settings)
        return {'path': str(settings_path(args.settings).resolve()), **value}

    items = preferences_from_stdin()
    software = {}
    if any(source.startswith("software-") for item in items for source in item["sources"]):
        if sys.platform != "win32":
            raise ValueError("Software evidence requires the original Windows host.")
        software = {item["id"]: item for item in scan_software()["software"]}
    for evidence_path in args.evidence:
        software.update(load_evidence(evidence_path))
    if args.connect:
        entries = []
        for item in items:
            sources = []
            for source in item["sources"]:
                if source in SETTINGS:
                    sources.append(f"Windows HKCU {SETTINGS[source][0]}::{SETTINGS[source][1]}")
                elif source in software:
                    evidence = software[source]
                    sources.append(f"{source} {evidence['name']} ({evidence['source']})")
                else:
                    raise ValueError("Evidence is no longer available. Scan and review again.")
            entries.append({"text": item["text"], "sources": sources or ["direct user statement"], "status": args.status,
                            **{key: item[key] for key in ("category", "detail", "confidence") if key in item}})
        arguments = {"entries": entries}
        if args.command == "save":
            arguments["review"] = args.review
        return call_lifecycle(args.connect, arguments)
    if any(set(item) - {"text", "sources"} for item in items):
        raise ValueError("Detailed profiles require --connect so details and provenance are preserved by the lifecycle service.")
    path = target_file(args.workspace)
    original = path.read_bytes()
    additions, review, lines, end, newline = proposal(path, original, items, software, args.status)
    result = {
        "path": str(path),
        "review": review,
        "additions": [{"text": wording, "sources": sources} for wording, sources in additions],
    }
    if args.command == "save":
        if not additions:
            return {**result, "saved": False}
        if args.review != review:
            raise ValueError("Proposal or memory changed since review. Preview again.")
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        block = newline.join(f"[{at}] Sources: {sources}.{newline}- {wording}" for wording, sources in additions)
        prefix = "".join(lines[:end])
        suffix = "".join(lines[end:])
        updated = prefix + newline * (1 if prefix.endswith(newline) else 2) + block + newline * (2 if suffix else 1) + suffix
        with workspace_writer(path.parent.parent):
            write_reviewed(path, original, updated.encode("utf-8"))
        result["saved"] = True
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        sys.exit(str(error))
