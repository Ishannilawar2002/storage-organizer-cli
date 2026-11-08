#!/usr/bin/env python3
"""A cautious, configurable local file organizer for macOS and removable drives.

The program is deliberately preview-first. It never permanently deletes files.
Moves require a saved plan and --yes; duplicate copies can be moved to a
reviewable quarantine and restored with rollback.
"""

import argparse
import collections
import datetime as _datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

VERSION = "0.2.0"

DEFAULT_CONFIG = {
    "version": 1,
    "roots": ["~/Desktop", "~/Downloads"],
    "state_dir": "~/.storage-organizer",
    "ignore_dir_names": [
        ".git", "node_modules", ".venv", "venv", "site-packages",
        ".Spotlight-V100", ".fseventsd", ".Trashes", ".Trash",
        ".TemporaryItems", "System Volume Information",
        "Storage Quarantine - Review",
    ],
    "protected_dir_names": [
        ".git", "node_modules", ".venv", "venv", "site-packages",
        "OllamaModels", "Photos Library.photoslibrary", "Photo Booth Library",
        "Music Library.musiclibrary",
    ],
    "ignore_globs": [
        "**/.Spotlight-V100/**", "**/.fseventsd/**", "**/.Trashes/**",
        "**/.TemporaryItems/**", "**/System Volume Information/**",
    ],
    "protected_globs": [
        "**/.git/**", "**/node_modules/**", "**/.venv/**", "**/venv/**",
        "**/site-packages/**", "**/OllamaModels/**",
        "**/*.photoslibrary/**", "**/*.musiclibrary/**",
        "**/Photo Booth Library/**", "**/Library/CloudStorage/**",
        "**/Applications/**", "**/System/**",
    ],
    "ignore_file_globs": ["._*", ".DS_Store", ".localized", "Thumbs.db", "desktop.ini"],
    "archive_extensions": [
        ".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz",
        ".dmg", ".iso", ".pkg",
    ],
    "quarantine_name": "Storage Quarantine - Review",
    "keeper_preference": ["Important Files", "Important Documents", "Pictures", "Books"],
    "moves": [],
    "rules": [
        {
            "name": "Desktop screenshots by month",
            "root": "~/Desktop",
            "match": {
                "extensions": [".png", ".jpg", ".jpeg"],
                "regex": "^Screenshot (?P<year>\\d{4})-(?P<month>\\d{2})-",
            },
            "destination": "~/Desktop/Screenshots/{year}-{month}",
        },
        {
            "name": "Desktop screen recordings by month",
            "root": "~/Desktop",
            "match": {
                "regex": "^Screen Recording (?P<year>\\d{4})-(?P<month>\\d{2})-",
            },
            "destination": "~/Desktop/Screen Recordings/{year}-{month}",
        },
        {
            "name": "Loose Desktop archives",
            "root": "~/Desktop",
            "match": {"extensions": [".zip", ".7z", ".rar"], "max_depth": 0},
            "destination": "~/Desktop/Archives/To Sort",
        },
    ],
}


def expand_path(value):
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def absolute_path(value):
    return Path(os.path.abspath(str(expand_path(value))))


def json_write(path, data):
    path = expand_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temp, path)


def read_json(path):
    with open(expand_path(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_config(path):
    config_path = expand_path(path)
    if not config_path.is_file():
        raise ValueError("Config not found: {}. Run `init` first.".format(config_path))
    config = read_json(config_path)
    if not isinstance(config, dict) or not isinstance(config.get("roots", []), list):
        raise ValueError("Config must be a JSON object with a roots list.")
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    for key in ("ignore_dir_names", "protected_dir_names", "ignore_globs",
                "protected_globs", "ignore_file_globs", "archive_extensions",
                "keeper_preference", "moves", "rules"):
        if key not in config:
            merged[key] = list(DEFAULT_CONFIG.get(key, []))
    return merged


def path_text(path):
    return os.path.abspath(str(path)).replace(os.sep, "/")


def is_within(path, root):
    path = os.path.abspath(str(path))
    root = os.path.abspath(str(root))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def roots_from_config(config, warn=True, allow_cloud_storage=False):
    roots = []
    for value in config.get("roots", []):
        root = absolute_path(value)
        if not root.is_dir():
            if warn:
                print("Skipping missing folder: {}".format(root), file=sys.stderr)
            continue
        if is_protected(root, config, allow_cloud_storage=allow_cloud_storage):
            if warn:
                print("Skipping protected root: {}".format(root), file=sys.stderr)
            continue
        roots.append(root)
    # Avoid scanning a nested root twice.
    roots = sorted(set(roots), key=lambda p: (len(str(p)), str(p).casefold()))
    result = []
    for root in roots:
        if not any(is_within(root, parent) for parent in result):
            result.append(root)
    return result


def _glob_match(path, pattern):
    text = path_text(path)
    pattern = os.path.expanduser(os.path.expandvars(str(pattern))).replace(os.sep, "/")
    if not os.path.isabs(pattern):
        return fnmatch.fnmatchcase(path.name.casefold(), pattern.casefold()) or \
            fnmatch.fnmatchcase(text.casefold(), pattern.casefold())
    return fnmatch.fnmatchcase(text.casefold(), pattern.casefold())


def _named(path, names):
    target = path.name.casefold()
    return any(target == str(name).casefold() for name in names)


def is_protected(path, config, allow_cloud_storage=False):
    path = Path(path)
    if any(_named(part, config.get("protected_dir_names", [])) for part in path.parents):
        return True
    if _named(path, config.get("protected_dir_names", [])):
        return True
    for pattern in config.get("protected_globs", []):
        if allow_cloud_storage and "cloudstorage" in str(pattern).casefold():
            continue
        if _glob_match(path, pattern):
            return True
    return False


def is_ignored_dir(path, config, allow_cloud_storage=False):
    quarantine_name = str(config.get("quarantine_name", "Storage Quarantine - Review")).casefold()
    if path.name.casefold() == quarantine_name or \
            _named(path, config.get("ignore_dir_names", [])) or \
            is_protected(path, config, allow_cloud_storage=allow_cloud_storage):
        return True
    return any(_glob_match(path, pattern) for pattern in config.get("ignore_globs", []))


def is_ignored_file(path, config):
    return any(_glob_match(path, pattern) for pattern in config.get("ignore_file_globs", []))


def walk_files(roots, config, include_protected=False, allow_cloud_storage=False, errors=None):
    seen = set()
    for root in roots:
        root = absolute_path(root)
        if not root.is_dir() or (not include_protected and
                                 is_protected(root, config, allow_cloud_storage=allow_cloud_storage)):
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError as exc:
                if errors is not None:
                    errors.append({"path": str(current), "error": str(exc)})
                print("Cannot read {}: {}".format(current, exc), file=sys.stderr)
                continue
            entries.sort(key=lambda entry: entry.name.casefold(), reverse=True)
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if is_ignored_dir(path, config, allow_cloud_storage=allow_cloud_storage):
                            continue
                        stack.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        if is_ignored_file(path, config):
                            continue
                        if not include_protected and is_protected(
                                path, config, allow_cloud_storage=allow_cloud_storage):
                            continue
                        key = os.path.normcase(os.path.abspath(str(path)))
                        if key not in seen:
                            seen.add(key)
                            yield path
                except OSError as exc:
                    if errors is not None:
                        errors.append({"path": str(path), "error": str(exc)})
                    continue


def file_kind(path, archive_extensions):
    suffix = path.suffix.casefold()
    if suffix in [str(x).casefold() for x in archive_extensions]:
        return "archive_or_installer"
    types = {
        ".pdf": "document", ".doc": "document", ".docx": "document",
        ".ppt": "document", ".pptx": "document", ".xls": "document",
        ".xlsx": "document", ".txt": "document", ".rtf": "document",
        ".png": "image", ".jpg": "image", ".jpeg": "image", ".heic": "image",
        ".gif": "image", ".webp": "image", ".tif": "image", ".tiff": "image",
        ".mp4": "video", ".mov": "video", ".mkv": "video", ".avi": "video",
        ".webm": "video", ".m4v": "video",
        ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".flac": "audio",
        ".aac": "audio", ".ogg": "audio",
        ".py": "code", ".js": "code", ".ts": "code", ".java": "code",
        ".c": "code", ".h": "code", ".cpp": "code", ".html": "code",
        ".css": "code", ".json": "code", ".yaml": "code", ".yml": "code",
    }
    return types.get(suffix, "other" if suffix else "no_extension")


def human_bytes(value):
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return "{:.2f} {}".format(value, unit)
        value /= 1000


def inventory(roots, config):
    counts = collections.Counter()
    sizes = collections.Counter()
    largest = []
    archives = []
    total = 0
    for path in walk_files(roots, config):
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        kind = file_kind(path, config.get("archive_extensions", []))
        counts[kind] += 1
        sizes[kind] += info.st_size
        total += info.st_size
        largest.append((info.st_size, str(path)))
        if kind == "archive_or_installer":
            archives.append({"path": str(path), "size": info.st_size, "extension": path.suffix})
    largest.sort(reverse=True)
    return {
        "created_at": _datetime.datetime.now().isoformat(timespec="seconds"),
        "roots": [str(x) for x in roots],
        "files": sum(counts.values()),
        "total_bytes": total,
        "by_kind": {key: {"files": counts[key], "bytes": sizes[key]} for key in sorted(counts)},
        "largest": [{"path": p, "bytes": s} for s, p in largest[:50]],
        "archives": archives,
    }


def _matches_file(path, info, root, match):
    extensions = match.get("extensions")
    if extensions and path.suffix.casefold() not in [str(x).casefold() for x in extensions]:
        return None
    glob = match.get("glob")
    if glob and not fnmatch.fnmatchcase(path.name.casefold(), str(glob).casefold()):
        return None
    regex = match.get("regex")
    groups = {}
    if regex:
        found = re.search(regex, path.name, re.IGNORECASE)
        if not found:
            return None
        groups = found.groupdict()
    if "min_size" in match and info.st_size < int(match["min_size"]):
        return None
    if "max_size" in match and info.st_size > int(match["max_size"]):
        return None
    max_depth = match.get("max_depth")
    if max_depth is not None:
        rel = Path(os.path.relpath(path.parent, root))
        depth = 0 if str(rel) == "." else len(rel.parts)
        if depth > int(max_depth):
            return None
    return groups


def _template_context(path, info, root, groups):
    stamp = _datetime.datetime.fromtimestamp(info.st_mtime)
    context = {
        "name": path.name,
        "stem": path.stem,
        "ext": path.suffix,
        "year": str(stamp.year),
        "month": "{:02d}".format(stamp.month),
        "day": "{:02d}".format(stamp.day),
        "root": str(root),
        "relative_dir": os.path.relpath(path.parent, root),
    }
    context.update({k: v for k, v in groups.items() if v is not None})
    return context


def _render_path(template, context, base):
    try:
        rendered = str(template).format(**context)
    except (KeyError, ValueError) as exc:
        raise ValueError("Bad template {!r}: {}".format(template, exc))
    path = expand_path(rendered)
    if not path.is_absolute():
        path = base / path
    return Path(os.path.abspath(str(path)))


def _protected_descendant(folder, config):
    if is_protected(folder, config):
        return True
    for base, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for name in dirs:
            if is_protected(Path(base) / name, config):
                return True
        for name in files:
            if is_protected(Path(base) / name, config):
                return True
    return False


def _action_status(src, dst, roots, config, allow_cloud_storage_source=False):
    if not any(is_within(src, root) for root in roots):
        return "blocked", "source is outside configured roots"
    if not any(is_within(dst, root) for root in roots):
        return "blocked", "destination is outside configured roots"
    if is_protected(src, config, allow_cloud_storage=allow_cloud_storage_source) or \
            is_protected(dst, config):
        return "blocked", "source or destination is protected"
    if os.path.abspath(str(src)) == os.path.abspath(str(dst)):
        return "skip", "source already has the requested location"
    if os.path.lexists(dst):
        return "conflict", "destination already exists"
    return "ready", ""


def build_plan(config):
    roots = roots_from_config(config)
    actions = []
    explicit_sources = []
    for item in config.get("moves", []):
        src = absolute_path(item["source"])
        dst = _render_path(item["destination"], {"name": src.name, "stem": src.stem,
                                  "ext": src.suffix, "year": "", "month": "",
                                  "day": ""}, src.parent)
        if not os.path.lexists(src):
            actions.append({"source": str(src), "destination": str(dst),
                            "reason": item.get("name", "configured move"),
                            "status": "blocked", "detail": "source does not exist"})
            continue
        if src.is_symlink() or src in roots:
            actions.append({"source": str(src), "destination": str(dst),
                            "reason": item.get("name", "configured move"),
                            "status": "blocked", "detail": "symlinks and root folders cannot be explicit moves"})
            continue
        if src.is_dir() and _protected_descendant(src, config):
            actions.append({"source": str(src), "destination": str(dst),
                            "reason": item.get("name", "configured move"),
                            "status": "blocked", "detail": "folder contains protected data"})
            continue
        status, detail = _action_status(src, dst, roots, config)
        actions.append({"source": str(src), "destination": str(dst),
                        "reason": item.get("name", "configured move"),
                        "status": status, "detail": detail,
                        "kind": "directory" if src.is_dir() else "file"})
        explicit_sources.append(src)

    for root in roots:
        for path in walk_files([root], config):
            if any(is_within(path, src) for src in explicit_sources):
                continue
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            for rule in config.get("rules", []):
                rule_root = absolute_path(rule.get("root", root))
                if not is_within(path, rule_root):
                    continue
                if is_protected(rule_root, config):
                    continue
                groups = _matches_file(path, info, rule_root, rule.get("match", {}))
                if groups is None:
                    continue
                context = _template_context(path, info, rule_root, groups)
                destination_dir = _render_path(rule["destination"], context, rule_root)
                rename = rule.get("rename")
                if rename:
                    new_name = str(rename).format(**context)
                    if not Path(new_name).suffix and path.suffix:
                        new_name += path.suffix
                    dst = destination_dir / new_name
                else:
                    dst = destination_dir / path.name
                status, detail = _action_status(path, dst, roots, config)
                actions.append({"source": str(path), "destination": str(dst),
                                "reason": rule.get("name", "configured rule"),
                                "status": status, "detail": detail, "kind": "file"})
                break

    # Mark duplicate destinations as conflicts before an apply can begin.
    by_dest = collections.defaultdict(list)
    for action in actions:
        if action.get("status") in ("ready", "conflict"):
            by_dest[os.path.normcase(action["destination"])].append(action)
    for same_dest in by_dest.values():
        if len(same_dest) > 1:
            for action in same_dest:
                action["status"] = "conflict"
                action["detail"] = "multiple sources target the same destination"

    policy = {key: config.get(key, []) for key in (
        "ignore_dir_names", "protected_dir_names", "ignore_globs", "protected_globs",
        "ignore_file_globs")}
    return {
        "version": 1,
        "tool_version": VERSION,
        "created_at": _datetime.datetime.now().isoformat(timespec="seconds"),
        "allowed_roots": [str(root) for root in roots],
        "policy": policy,
        "actions": actions,
    }


def _sidecar(path):
    return path.with_name("._" + path.name)


def _nearest_existing(path):
    current = Path(path)
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _same_device(src, dst):
    try:
        source_device = os.lstat(src).st_dev
        destination_device = os.stat(_nearest_existing(Path(dst).parent)).st_dev
        return source_device == destination_device
    except OSError:
        return False


def _ensure_parent(path, created_dirs):
    missing = []
    current = Path(path)
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        created_dirs.append(str(directory))


def _move_single(src, dst, journal, expected_identity=None):
    src = Path(src)
    dst = Path(dst)
    if not os.path.lexists(src):
        raise FileNotFoundError(str(src))
    if src.is_symlink():
        raise ValueError("Refusing to move a symlink: {}".format(src))
    if expected_identity is not None:
        current = os.lstat(src)
        identity = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        if identity != expected_identity:
            raise ValueError("File changed after its duplicate hash was checked.")
    if os.path.lexists(dst):
        raise FileExistsError(str(dst))
    if not _same_device(src, dst):
        raise ValueError("Cross-volume move refused: {} -> {}".format(src, dst))
    dst_sidecar = _sidecar(dst)
    src_sidecar = _sidecar(src)
    has_sidecar = os.path.lexists(src_sidecar)
    if has_sidecar and os.path.lexists(dst_sidecar):
        raise FileExistsError(str(dst_sidecar))
    _ensure_parent(dst.parent, journal["created_dirs"])
    os.rename(src, dst)
    journal["moves"].append({"source": str(src), "destination": str(dst)})
    journal["updated_at"] = _datetime.datetime.now().isoformat(timespec="seconds")
    if has_sidecar:
        os.rename(src_sidecar, dst_sidecar)
        journal["moves"].append({"source": str(src_sidecar), "destination": str(dst_sidecar)})
    return journal


def _journal_save(path, journal):
    json_write(path, journal)


def _copy_verified(src, dst, journal, expected_size, expected_hash, block_size):
    src = Path(src)
    dst = Path(dst)
    if src.is_symlink() or not os.path.lexists(src):
        raise ValueError("Copy source is missing or is a symlink.")
    before = os.lstat(src)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Only regular files can be localized.")
    if before.st_size != int(expected_size):
        raise ValueError("Source size changed after the reconciliation report.")
    if os.path.lexists(dst):
        raise FileExistsError(str(dst))
    _ensure_parent(dst.parent, journal["created_dirs"])
    temp = dst.with_name(dst.name + ".storage-organizer-partial")
    if os.path.lexists(temp):
        raise FileExistsError("Temporary destination already exists: {}".format(temp))
    digest = hashlib.sha256()
    copied = 0
    try:
        with open(src, "rb") as source, open(temp, "xb") as target:
            opened = os.fstat(source.fileno())
            opened_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            if opened_identity != before_identity:
                raise ValueError("Source changed before copy began.")
            while True:
                chunk = source.read(block_size)
                if not chunk:
                    break
                written = target.write(chunk)
                if written != len(chunk):
                    raise OSError("Short write while copying {}".format(src))
                digest.update(chunk)
                copied += len(chunk)
            target.flush()
            os.fsync(target.fileno())
            after_open = os.fstat(source.fileno())
        after_path = os.stat(src, follow_symlinks=False)
        after_identity = (after_path.st_dev, after_path.st_ino, after_path.st_size, after_path.st_mtime_ns)
        if copied != int(expected_size) or digest.hexdigest() != expected_hash:
            raise ValueError("Copied bytes do not match the planned SHA-256 or size.")
        opened_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        after_open_identity = (after_open.st_dev, after_open.st_ino, after_open.st_size, after_open.st_mtime_ns)
        if opened_identity != after_open_identity or opened_identity != after_identity:
            raise ValueError("Source changed while copying.")
        if os.path.lexists(dst):
            raise FileExistsError(str(dst))
        shutil.copystat(src, temp, follow_symlinks=False)
        os.rename(temp, dst)
        journal["copies"].append({
            "source": str(src), "destination": str(dst),
            "sha256": expected_hash, "size": int(expected_size),
        })
        journal["updated_at"] = _datetime.datetime.now().isoformat(timespec="seconds")
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def apply_plan(plan_path, journal_path, yes):
    plan = read_json(plan_path)
    actions = plan.get("actions", [])
    ready = [a for a in actions if a.get("status") == "ready"]
    blocked = len(actions) - len(ready)
    if not yes:
        print("Preview only: {} actions are ready; {} are skipped or blocked.".format(len(ready), blocked))
        print("To apply this saved plan, rerun with --yes.")
        return 0
    if not ready:
        print("No ready actions in this plan.")
        return 0
    journal_path = expand_path(journal_path)
    if os.path.lexists(journal_path):
        raise FileExistsError("Journal already exists: {}".format(journal_path))
    journal = {
        "version": 1,
        "tool_version": VERSION,
        "plan": str(expand_path(plan_path)),
        "created_at": _datetime.datetime.now().isoformat(timespec="seconds"),
        "moves": [], "copies": [], "created_dirs": [], "errors": [],
    }
    _journal_save(journal_path, journal)
    roots = [absolute_path(x) for x in plan.get("allowed_roots", [])]
    policy = dict(DEFAULT_CONFIG)
    policy.update(plan.get("policy", {}))
    for action in ready:
        src = absolute_path(action["source"])
        dst = absolute_path(action["destination"])
        kind = action.get("kind", "move")
        allow_cloud_source = bool(action.get("allow_cloud_source", False))
        try:
            if not any(is_within(src, root) for root in roots) or \
                    not any(is_within(dst, root) for root in roots):
                raise ValueError("Action is outside an allowed root.")
            if is_protected(src, policy, allow_cloud_storage=allow_cloud_source) or \
                    is_protected(dst, policy):
                raise ValueError("Action touches a protected path.")
            if not os.path.lexists(src) or src.is_symlink():
                raise ValueError("Source is missing or is a symlink.")
            if kind != "copy" and src.is_dir() and _protected_descendant(src, policy):
                raise ValueError("Folder contains protected data.")
            if kind == "copy":
                _copy_verified(src, dst, journal, action["expected_size"],
                               action["expected_sha256"],
                               int(plan.get("hash_block_size", 1024 * 1024)))
                _journal_save(journal_path, journal)
                print("Copied and verified: {} -> {}".format(src, dst))
                continue
            expected_identity = None
            expected_hash = action.get("expected_sha256")
            if expected_hash:
                before = os.lstat(src)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Hash-verified quarantine only accepts regular files.")
                if before.st_size != int(action.get("expected_size", -1)):
                    raise ValueError("File size changed after the duplicate report.")
                digest = sha256_file(src, int(plan.get("hash_block_size", 1024 * 1024)))
                after = os.lstat(src)
                expected_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                if expected_identity != after_identity or digest != expected_hash:
                    raise ValueError("File no longer matches the duplicate report; skipped.")
            _move_single(src, dst, journal, expected_identity)
            _journal_save(journal_path, journal)
            print("Moved: {} -> {}".format(src, dst))
        except Exception as exc:
            journal["errors"].append({"source": str(src), "destination": str(dst), "error": str(exc)})
            _journal_save(journal_path, journal)
            print("Skipped: {} ({})".format(src, exc), file=sys.stderr)
    journal["finished_at"] = _datetime.datetime.now().isoformat(timespec="seconds")
    _journal_save(journal_path, journal)
    print("Journal: {}".format(journal_path))
    moved = len([m for m in journal["moves"] if not Path(m["source"]).name.startswith("._")])
    copied = len(journal["copies"])
    print("Completed {} moves and {} verified copies; {} errors.".format(
        moved, copied, len(journal["errors"])))
    return 1 if journal["errors"] else 0


def rollback(journal_path, yes):
    journal_path = expand_path(journal_path)
    journal = read_json(journal_path)
    moves = journal.get("moves", [])
    copies = journal.get("copies", [])
    if not yes:
        print("Preview only: {} moves can be reversed and {} created copies can be removed.".format(
            len(moves), len(copies)))
        print("To reverse them, rerun with --yes.")
        return 0
    errors = []
    for item in reversed(copies):
        destination = Path(item["destination"])
        try:
            if not os.path.lexists(destination):
                continue
            info = os.lstat(destination)
            if destination.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise ValueError("Created copy is no longer a regular file.")
            if info.st_size != int(item["size"]):
                raise ValueError("Created copy changed since the run.")
            if sha256_file(destination, 1024 * 1024) != item["sha256"]:
                raise ValueError("Created copy content changed since the run.")
            current = os.lstat(destination)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != \
                    (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                raise ValueError("Created copy changed while checking rollback.")
            os.unlink(destination)
        except Exception as exc:
            errors.append({"destination": str(destination), "error": str(exc)})
            print("Could not remove created copy {}: {}".format(destination, exc), file=sys.stderr)
    for item in reversed(moves):
        src = Path(item["destination"])
        dst = Path(item["source"])
        try:
            if not os.path.lexists(src):
                raise FileNotFoundError("Moved item is missing: {}".format(src))
            if os.path.lexists(dst):
                raise FileExistsError("Original path is occupied: {}".format(dst))
            if src.is_symlink() or not _same_device(src, dst):
                raise ValueError("Refusing unsafe or cross-volume rollback.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
        except Exception as exc:
            errors.append({"source": str(src), "destination": str(dst), "error": str(exc)})
            print("Could not restore {}: {}".format(src, exc), file=sys.stderr)
    for directory in reversed(journal.get("created_dirs", [])):
        try:
            os.rmdir(directory)
        except (FileNotFoundError, OSError):
            pass
    journal["rollback_errors"] = errors
    journal["rolled_back_at"] = _datetime.datetime.now().isoformat(timespec="seconds")
    _journal_save(journal_path, journal)
    print("Rollback finished with {} errors.".format(len(errors)))
    return 1 if errors else 0


def sha256_file(path, block_size):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def duplicate_report(roots, config, min_size=0, max_size=None):
    by_size = collections.defaultdict(list)
    scanned = 0
    errors = []
    for path in walk_files(roots, config):
        try:
            info = os.stat(path, follow_symlinks=False)
            if info.st_size < int(min_size):
                continue
            if max_size is not None and info.st_size > int(max_size):
                continue
            by_size[info.st_size].append({
                "path": str(path), "size": info.st_size, "device": info.st_dev,
                "inode": info.st_ino, "mtime_ns": getattr(info, "st_mtime_ns", 0),
            })
            scanned += 1
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    groups = []
    hashed = 0
    block_size = int(config.get("hash_block_size", 1024 * 1024))
    for size, candidates in by_size.items():
        if len(candidates) < 2:
            continue
        by_hash = collections.defaultdict(list)
        for item in candidates:
            try:
                current = os.stat(item["path"], follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or current.st_size != size:
                    continue
                before_identity = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                item["sha256"] = sha256_file(item["path"], block_size)
                after = os.stat(item["path"], follow_symlinks=False)
                after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                if before_identity != after_identity:
                    errors.append({"path": item["path"], "error": "file changed while hashing"})
                    continue
                item["device"] = after.st_dev
                item["inode"] = after.st_ino
                item["mtime_ns"] = after.st_mtime_ns
                by_hash[item["sha256"]].append(item)
                hashed += 1
                if hashed % 1000 == 0:
                    print("Hashed {} duplicate candidates...".format(hashed), file=sys.stderr)
            except OSError as exc:
                errors.append({"path": item["path"], "error": str(exc)})
        for digest, same in by_hash.items():
            if len(same) < 2:
                continue
            objects = {(x["device"], x["inode"]) for x in same}
            groups.append({
                "sha256": digest,
                "size": size,
                "path_count": len(same),
                "unique_file_objects": len(objects),
                "duplicate_paths": len(same) - 1,
                "reclaimable_bytes": max(0, len(objects) - 1) * size,
                "paths": same,
            })
    groups.sort(key=lambda g: (g["reclaimable_bytes"], g["size"]), reverse=True)
    return {
        "version": 1,
        "created_at": _datetime.datetime.now().isoformat(timespec="seconds"),
        "roots": [str(root) for root in roots],
        "scanned_files_in_size_range": scanned,
        "hashed_candidate_files": hashed,
        "groups": groups,
        "duplicate_paths": sum(g["duplicate_paths"] for g in groups),
        "reclaimable_bytes": sum(g["reclaimable_bytes"] for g in groups),
        "read_errors": errors,
    }


def duplicate_quarantine_plan(report, config):
    roots = roots_from_config(config)
    actions = []
    quarantine_name = config.get("quarantine_name", "Storage Quarantine - Review")
    preferences = [str(x).casefold() for x in config.get("keeper_preference", [])]
    for group in report.get("groups", []):
        paths = [item for item in group.get("paths", [])
                 if os.path.lexists(item["path"]) and
                 any(is_within(item["path"], root) for root in roots)]
        protected = [item for item in paths if is_protected(item["path"], config)]
        candidates = [item for item in paths if item not in protected]
        if protected:
            keepers = protected
            extras = candidates
        elif candidates:
            def keeper_score(item):
                text = item["path"].casefold()
                rank = len(preferences)
                for i, term in enumerate(preferences):
                    if term and term in text:
                        rank = i
                        break
                return (rank, len(text), text)
            candidates.sort(key=keeper_score)
            keepers = [candidates[0]]
            extras = candidates[1:]
        else:
            continue
        keeper_objects = {(x.get("device"), x.get("inode")) for x in keepers}
        for item in extras:
            identity = (item.get("device"), item.get("inode"))
            if identity in keeper_objects:
                continue
            path = absolute_path(item["path"])
            matching_roots = [root for root in roots if is_within(path, root)]
            if not matching_roots:
                continue
            root = max(matching_roots, key=lambda p: len(str(p)))
            relative = Path(os.path.relpath(path, root))
            root_tag = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
            destination = root / quarantine_name / (root.name + "-" + root_tag) / relative
            status, detail = _action_status(path, destination, roots, config)
            actions.append({
                "source": str(path), "destination": str(destination),
                "reason": "Exact SHA-256 duplicate {}".format(group["sha256"]),
                "expected_sha256": group["sha256"],
                "expected_size": group["size"],
                "status": status, "detail": detail, "kind": "file",
            })
    policy = {key: config.get(key, []) for key in (
        "ignore_dir_names", "protected_dir_names", "ignore_globs", "protected_globs",
        "ignore_file_globs")}
    return {
        "version": 1,
        "tool_version": VERSION,
        "created_at": _datetime.datetime.now().isoformat(timespec="seconds"),
        "allowed_roots": [str(root) for root in roots],
        "policy": policy,
        "actions": actions,
        "note": "Duplicate copies are moved to a review quarantine, never permanently deleted.",
    }


def _archive_extension(name, extensions):
    low = name.casefold()
    for ext in sorted((str(x).casefold() for x in extensions), key=len, reverse=True):
        if low.endswith(ext):
            return ext
    return ""


def archive_index(roots, config):
    extensions = config.get("archive_extensions", [])
    parts = collections.defaultdict(list)
    rows = []
    part_pattern = re.compile(r"(?i)^(.*?)(?:[-_.](?:part[-_.]?)?\d{1,2})?[-_.](\d{3,4})\.(zip|7z|rar)$")
    for path in walk_files(roots, config):
        ext = _archive_extension(path.name, extensions)
        if not ext:
            continue
        try:
            size = os.stat(path, follow_symlinks=False).st_size
        except OSError:
            continue
        row = {"path": str(path), "size": size, "extension": ext, "multipart_group": None}
        if ext in (".zip", ".7z", ".rar"):
            match = part_pattern.match(path.name)
            if match:
                key = (str(path.parent), match.group(1).casefold(), match.group(3).casefold())
                parts[key].append(row)
        rows.append(row)
    for key, group in parts.items():
        if len(group) > 1:
            label = "{} [{} parts]".format(group[0]["path"], len(group))
            for row in group:
                row["multipart_group"] = label
    rows.sort(key=lambda x: (x["path"].casefold()))
    return {
        "version": 1,
        "created_at": _datetime.datetime.now().isoformat(timespec="seconds"),
        "roots": [str(root) for root in roots],
        "archive_count": len(rows),
        "total_bytes": sum(x["size"] for x in rows),
        "archives": rows,
    }


def clone_check(first, second, config, with_hash=False, tolerance=2):
    first = absolute_path(first)
    second = absolute_path(second)
    if not first.is_dir() or not second.is_dir():
        raise ValueError("Both clone-check paths must be existing folders.")

    def index(root):
        out = {}
        for path in walk_files([root], config):
            try:
                info = os.stat(path, follow_symlinks=False)
                out[os.path.relpath(path, root)] = {
                    "path": str(path), "size": info.st_size,
                    "mtime": info.st_mtime,
                }
            except OSError:
                continue
        return out

    a = index(first)
    b = index(second)
    same_path = sorted(set(a) & set(b))
    same_size = [rel for rel in same_path if a[rel]["size"] == b[rel]["size"]]
    timestamp_match = [rel for rel in same_size
                       if abs(a[rel]["mtime"] - b[rel]["mtime"]) <= float(tolerance)]
    identical = []
    mismatched = []
    if with_hash:
        block = int(config.get("hash_block_size", 1024 * 1024))
        for rel in same_size:
            try:
                if sha256_file(a[rel]["path"], block) == sha256_file(b[rel]["path"], block):
                    identical.append(rel)
                else:
                    mismatched.append(rel)
            except OSError as exc:
                mismatched.append(rel)
                print("Could not hash {}: {}".format(rel, exc), file=sys.stderr)
    return {
        "created_at": _datetime.datetime.now().isoformat(timespec="seconds"),
        "folder_a": str(first), "folder_b": str(second),
        "files_a": len(a), "files_b": len(b),
        "same_relative_paths": len(same_path),
        "same_path_and_size": len(same_size),
        "same_path_size_and_timestamp": len(timestamp_match),
        "hashes_checked": len(same_size) if with_hash else 0,
        "identical_hashes": len(identical),
        "different_hashes": len(mismatched),
        "only_in_a": sorted(set(a) - set(b))[:1000],
        "only_in_b": sorted(set(b) - set(a))[:1000],
        "same_paths_different_size": sorted(rel for rel in same_path if a[rel]["size"] != b[rel]["size"])[:1000],
        "hash_identical_paths": identical[:1000],
    }


def _hash_tree(root, config, allow_cloud_storage=False):
    index = collections.defaultdict(list)
    errors = []
    hashed = 0
    block_size = int(config.get("hash_block_size", 1024 * 1024))
    for path in walk_files([root], config, allow_cloud_storage=allow_cloud_storage, errors=errors):
        try:
            before = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                continue
            identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            digest = sha256_file(path, block_size)
            after = os.stat(path, follow_symlinks=False)
            after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity != after_identity:
                errors.append({"path": str(path), "error": "file changed while hashing"})
                continue
            relative = os.path.relpath(path, root)
            index[digest].append({
                "path": str(path), "relative_path": relative, "size": after.st_size,
                "sha256": digest, "device": after.st_dev, "inode": after.st_ino,
                "mtime_ns": after.st_mtime_ns,
            })
            hashed += 1
            if hashed % 1000 == 0:
                print("Hashed {} files under {}...".format(hashed, root), file=sys.stderr)
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return index, errors, hashed


def reconcile_cloud_local(cloud_root, local_root, config):
    cloud_root = absolute_path(cloud_root)
    local_root = absolute_path(local_root)
    if not cloud_root.is_dir() or not local_root.is_dir():
        raise ValueError("Cloud and local paths must both be existing folders.")
    if is_within(cloud_root, local_root) or is_within(local_root, cloud_root):
        raise ValueError("Cloud and local roots must be separate, non-overlapping folders.")
    allowed_roots = roots_from_config(config, allow_cloud_storage=True)
    if not any(is_within(cloud_root, root) for root in allowed_roots):
        raise ValueError("Cloud root is outside configured roots; add it to roots first.")
    if not any(is_within(local_root, root) for root in allowed_roots):
        raise ValueError("Local HDD folder is outside configured roots; add it to roots first.")
    cloud_index, cloud_errors, cloud_count = _hash_tree(cloud_root, config, allow_cloud_storage=True)
    local_index, local_errors, local_count = _hash_tree(local_root, config)

    cloud_matches_local = []
    cloud_only = []
    local_only = []
    cloud_removal_candidates = []
    all_hashes = sorted(set(cloud_index) | set(local_index))
    for digest in all_hashes:
        cloud_files = sorted(cloud_index.get(digest, []), key=lambda x: x["relative_path"].casefold())
        local_files = sorted(local_index.get(digest, []), key=lambda x: x["relative_path"].casefold())
        group = {
            "sha256": digest,
            "size": (cloud_files or local_files)[0]["size"],
            "cloud_paths": cloud_files,
            "local_paths": local_files,
        }
        if cloud_files and local_files:
            cloud_matches_local.append(group)
            for item in cloud_files:
                cloud_removal_candidates.append({
                    "path": item["path"], "relative_path": item["relative_path"],
                    "size": item["size"], "sha256": digest,
                    "local_matches": [x["path"] for x in local_files],
                    "reason": "Exact content already exists in the local tree",
                })
        elif cloud_files:
            cloud_only.append(group)
            if len(cloud_files) > 1:
                for item in cloud_files[1:]:
                    cloud_removal_candidates.append({
                        "path": item["path"], "relative_path": item["relative_path"],
                        "size": item["size"], "sha256": digest,
                        "local_matches": [cloud_files[0]["path"]],
                        "reason": "Exact duplicate of another cloud path",
                    })
        elif local_files:
            local_only.append(group)
    return {
        "version": 1,
        "created_at": _datetime.datetime.now().isoformat(timespec="seconds"),
        "cloud_root": str(cloud_root),
        "local_root": str(local_root),
        "hash_algorithm": "SHA-256",
        "cloud_files_hashed": cloud_count,
        "local_files_hashed": local_count,
        "cloud_matches_local": cloud_matches_local,
        "cloud_only_content": cloud_only,
        "local_only_content": local_only,
        "cloud_removal_candidates_for_review": cloud_removal_candidates,
        "potential_localize_bytes": sum(g["size"] for g in cloud_only),
        "read_errors": cloud_errors + local_errors,
        "note": "Read-only comparison. Hashing a streamed cloud file may download it to the provider cache. No cloud files are removed.",
    }


def localize_plan_from_reconcile(report, config):
    roots = roots_from_config(config, allow_cloud_storage=True)
    cloud_root = absolute_path(report["cloud_root"])
    local_root = absolute_path(report["local_root"])
    if not any(is_within(cloud_root, root) for root in roots) or \
            not any(is_within(local_root, root) for root in roots):
        raise ValueError("The report roots must still be included in the current config.")
    actions = []
    preferences = [str(x).casefold() for x in config.get("keeper_preference", [])]
    for group in report.get("cloud_only_content", []):
        files = list(group.get("cloud_paths", []))
        if not files:
            continue

        def source_score(item):
            text = item["path"].casefold()
            rank = len(preferences)
            for i, term in enumerate(preferences):
                if term and term in text:
                    rank = i
                    break
            return (rank, len(text), text)

        files.sort(key=source_score)
        source = absolute_path(files[0]["path"])
        relative = Path(files[0]["relative_path"])
        if not is_within(source, cloud_root):
            raise ValueError("Report source is outside its cloud root: {}".format(source))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe relative path in reconciliation report: {}".format(relative))
        destination = absolute_path(local_root / relative)
        if not is_within(destination, local_root):
            raise ValueError("Report destination is outside the local root: {}".format(destination))
        if os.path.lexists(destination):
            try:
                info = os.stat(destination, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_size == int(group["size"]) and \
                        sha256_file(destination, int(config.get("hash_block_size", 1024 * 1024))) == group["sha256"]:
                    status, detail = "skip", "identical file is already in the local folder"
                else:
                    status, detail = "conflict", "destination exists with different content"
            except OSError as exc:
                status, detail = "conflict", "cannot inspect destination: {}".format(exc)
        else:
            if not os.path.lexists(source) or source.is_symlink() or not source.is_file():
                status, detail = "blocked", "cloud source is missing or is not a regular file"
            else:
                status, detail = _action_status(
                    source, destination, roots, config, allow_cloud_storage_source=True)
        actions.append({
            "source": str(source), "destination": str(destination),
            "reason": "Localize unique cloud content; preserve relative path",
            "status": status, "detail": detail, "kind": "copy",
            "expected_sha256": group["sha256"], "expected_size": int(group["size"]),
            "allow_cloud_source": True,
        })
    policy = {key: config.get(key, []) for key in (
        "ignore_dir_names", "protected_dir_names", "ignore_globs", "protected_globs",
        "ignore_file_globs")}
    return {
        "version": 1,
        "tool_version": VERSION,
        "created_at": _datetime.datetime.now().isoformat(timespec="seconds"),
        "allowed_roots": [str(root) for root in roots],
        "policy": policy,
        "hash_block_size": int(config.get("hash_block_size", 1024 * 1024)),
        "actions": actions,
        "note": "Copies unique cloud content to local storage. Source cloud files are never deleted.",
    }


def default_output(stem, suffix="json"):
    stamp = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("{}-{}.{}".format(stem, stamp, suffix))


def print_plan(plan, output):
    counts = collections.Counter(action.get("status", "unknown") for action in plan.get("actions", []))
    print("Plan saved to {}".format(output))
    print("{} ready, {} conflicts, {} blocked, {} skipped".format(
        counts["ready"], counts["conflict"], counts["blocked"], counts["skip"]))
    for action in plan.get("actions", []):
        if action.get("status") == "ready":
            verb = "COPY" if action.get("kind") == "copy" else "MOVE"
            print("{}  {}  ->  {}  [{}]".format(
                verb, action["source"], action["destination"], action.get("reason", "")))
        elif action.get("status") not in ("skip",):
            print("{}  {}  ({})".format(action.get("status", "?"), action["source"],
                                        action.get("detail", "")))


def command_init(args):
    target = expand_path(args.config)
    if os.path.lexists(target):
        raise FileExistsError("Will not overwrite existing config: {}".format(target))
    json_write(target, DEFAULT_CONFIG)
    print("Created {}".format(target))
    print("Edit roots and rules before planning moves. Plans are previews until explicitly applied.")
    return 0


def command_inventory(args):
    config = load_config(args.config)
    report = inventory(roots_from_config(config), config)
    out = expand_path(args.out) if args.out else default_output("storage-inventory")
    json_write(out, report)
    print("{} files, {} total".format(report["files"], human_bytes(report["total_bytes"])))
    print("Inventory: {}".format(out))
    for kind, values in report["by_kind"].items():
        print("  {:20} {:7} files  {}".format(kind, values["files"], human_bytes(values["bytes"])))
    print("Largest files:")
    for item in report["largest"][:10]:
        print("  {}  {}".format(human_bytes(item["bytes"]), item["path"]))
    return 0


def command_plan(args):
    config = load_config(args.config)
    plan = build_plan(config)
    out = expand_path(args.out) if args.out else default_output("storage-plan")
    json_write(out, plan)
    print_plan(plan, out)
    return 0


def command_apply(args):
    plan = expand_path(args.plan)
    if args.journal:
        journal = expand_path(args.journal)
    else:
        state_dir = expand_path(args.state_dir or "~/.storage-organizer/journals")
        journal = state_dir / ("journal-" + _datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    return apply_plan(plan, journal, args.yes)


def command_rollback(args):
    return rollback(args.journal, args.yes)


def command_duplicates(args):
    config = load_config(args.config)
    report = duplicate_report(roots_from_config(config), config, args.min_size, args.max_size)
    out = expand_path(args.out) if args.out else default_output("duplicate-report")
    json_write(out, report)
    print("{} exact-duplicate groups; {} duplicate paths; about {} reclaimable".format(
        len(report["groups"]), report["duplicate_paths"], human_bytes(report["reclaimable_bytes"])))
    print("Full SHA-256 report: {}".format(out))
    for group in report["groups"][:15]:
        print("  {}  {} paths  {}".format(human_bytes(group["size"]), group["path_count"], group["sha256"][:16]))
        for item in group["paths"][:4]:
            print("    {}".format(item["path"]))
    return 0


def command_quarantine_plan(args):
    config = load_config(args.config)
    report = read_json(args.report)
    plan = duplicate_quarantine_plan(report, config)
    out = expand_path(args.out) if args.out else default_output("quarantine-plan")
    json_write(out, plan)
    print_plan(plan, out)
    print("Quarantine plans only move files; nothing is permanently deleted.")
    return 0


def command_archives(args):
    config = load_config(args.config)
    report = archive_index(roots_from_config(config), config)
    out = expand_path(args.out) if args.out else default_output("archive-index")
    json_write(out, report)
    print("{} archive/installers, {} total".format(report["archive_count"], human_bytes(report["total_bytes"])))
    print("Archive index: {}".format(out))
    for item in report["archives"]:
        suffix = "  [multipart: {}]".format(item["multipart_group"]) if item["multipart_group"] else ""
        print("  {}  {}{}".format(human_bytes(item["size"]), item["path"], suffix))
    return 0


def command_clone_check(args):
    config = load_config(args.config)
    report = clone_check(args.folder_a, args.folder_b, config, args.hash, args.timestamp_tolerance)
    out = expand_path(args.out) if args.out else default_output("folder-compare")
    json_write(out, report)
    print("A: {} files; B: {} files".format(report["files_a"], report["files_b"]))
    print("{} paths match; {} also match size; {} timestamps match".format(
        report["same_relative_paths"], report["same_path_and_size"], report["same_path_size_and_timestamp"]))
    if args.hash:
        print("{} SHA-256 matches; {} different hashes".format(
            report["identical_hashes"], report["different_hashes"]))
    print("Comparison: {}".format(out))
    return 0


def command_reconcile(args):
    config = load_config(args.config)
    report = reconcile_cloud_local(args.cloud_root, args.local_root, config)
    out = expand_path(args.out) if args.out else default_output("cloud-local-reconcile")
    json_write(out, report)
    print("Cloud files hashed: {}; local files hashed: {}".format(
        report["cloud_files_hashed"], report["local_files_hashed"]))
    print("{} cloud content groups already exist locally.".format(len(report["cloud_matches_local"])))
    print("{} unique cloud content groups need a local copy ({}).".format(
        len(report["cloud_only_content"]), human_bytes(report["potential_localize_bytes"])))
    print("{} cloud-side paths are exact-copy candidates for review.".format(
        len(report["cloud_removal_candidates_for_review"])))
    print("No cloud items were removed. Report: {}".format(out))
    return 0


def command_localize_plan(args):
    config = load_config(args.config)
    report = read_json(args.report)
    plan = localize_plan_from_reconcile(report, config)
    out = expand_path(args.out) if args.out else default_output("localize-plan")
    json_write(out, plan)
    print_plan(plan, out)
    print("Applying this plan copies unique content to the local folder; source cloud files stay in place.")
    return 0


def make_parser():
    parser = argparse.ArgumentParser(
        prog="storage-organizer",
        description="Preview-first file organization, cloud/local reconciliation, and rollback.")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter JSON configuration")
    p.add_argument("--config", default="storage-organizer.json")
    p.set_defaults(func=command_init)

    p = sub.add_parser("inventory", help="summarize files and archive sizes")
    p.add_argument("--config", default="storage-organizer.json")
    p.add_argument("--out")
    p.set_defaults(func=command_inventory)

    p = sub.add_parser("plan", help="preview configured file and folder moves")
    p.add_argument("--config", default="storage-organizer.json")
    p.add_argument("--out")
    p.set_defaults(func=command_plan)

    p = sub.add_parser("apply", help="apply a previously saved plan")
    p.add_argument("--plan", required=True)
    p.add_argument("--journal")
    p.add_argument("--state-dir")
    p.add_argument("--yes", action="store_true", help="required to make moves")
    p.set_defaults(func=command_apply)

    p = sub.add_parser("rollback", help="preview or reverse a journal")
    p.add_argument("--journal", required=True)
    p.add_argument("--yes", action="store_true", help="required to reverse moves")
    p.set_defaults(func=command_rollback)

    p = sub.add_parser("duplicates", help="find exact duplicate content by SHA-256")
    p.add_argument("--config", default="storage-organizer.json")
    p.add_argument("--min-size", type=int, default=0)
    p.add_argument("--max-size", type=int)
    p.add_argument("--out")
    p.set_defaults(func=command_duplicates)

    p = sub.add_parser("quarantine-plan", help="plan reversible quarantine moves from a duplicate report")
    p.add_argument("--config", default="storage-organizer.json")
    p.add_argument("--report", required=True)
    p.add_argument("--out")
    p.set_defaults(func=command_quarantine_plan)

    p = sub.add_parser("archives", help="catalog ZIP/RAR/DMG files and likely multipart groups")
    p.add_argument("--config", default="storage-organizer.json")
    p.add_argument("--out")
    p.set_defaults(func=command_archives)

    p = sub.add_parser("clone-check", help="compare two folder trees without changing them")
    p.add_argument("folder_a")
    p.add_argument("folder_b")
    p.add_argument("--config", default="storage-organizer.json")
    p.add_argument("--hash", action="store_true", help="SHA-256 common path/size matches")
    p.add_argument("--timestamp-tolerance", type=float, default=2.0)
    p.add_argument("--out")
    p.set_defaults(func=command_clone_check)

    p = sub.add_parser("reconcile", help="hash-compare a mounted cloud folder with a local folder")
    p.add_argument("--cloud-root", required=True)
    p.add_argument("--local-root", required=True)
    p.add_argument("--config", default="storage-organizer.json")
    p.add_argument("--out")
    p.set_defaults(func=command_reconcile)

    p = sub.add_parser("localize-plan", help="plan verified copies of cloud-only content onto local storage")
    p.add_argument("--report", required=True)
    p.add_argument("--config", default="storage-organizer.json")
    p.add_argument("--out")
    p.set_defaults(func=command_localize_plan)

    return parser


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
