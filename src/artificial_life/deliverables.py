"""Persist model-authored work files in a fresh activity directory; never execute them."""
import hashlib
from pathlib import PurePosixPath
import re
from uuid import uuid4


def validate(files):
    if not isinstance(files, list) or len(files) > 12:
        raise ValueError("deliverables must contain at most 12 files")
    names, total = set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"name", "content"}:
            raise ValueError("Deliverable needs name and content")
        name, content = item["name"], item["content"]
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", name)
                or PurePosixPath(name).suffix.lower() not in {".md", ".txt", ".html", ".css", ".js", ".py", ".json", ".csv", ".svg"}
                or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}
                or name.lower() in names):
            raise ValueError("Deliverable requires a unique portable filename and supported suffix")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Deliverable content must be nonempty text")
        names.add(name.lower())
        total += len(content.encode("utf-8"))
    if total > 256000:
        raise ValueError("Deliverables exceed 256 KB")
    return files


def persist(root, activity_id, files):
    validate(files)
    if not files:
        return []
    # Fresh bundle: retries never overwrite earlier files.
    folder = root / "works/autonomous" / ("bundle-" + uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    result = []
    for item in files:
        path = folder / item["name"]
        with path.open("xb") as stream:
            stream.write(item["content"].encode("utf-8"))
        result.append({"name": item["name"], "path": str(path.relative_to(root)),
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                       "activity_id": activity_id, "execution_verified": False})
    return result
