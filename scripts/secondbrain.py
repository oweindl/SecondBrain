#!/usr/bin/env python3
"""Optional local reliability adapter; no host tools, installation or polling on import.

Locks coordinate this adapter on one machine only. Hash rechecks do not eliminate
the check/replace race with noncooperating editors or cloud synchronization.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import struct
import sys
import tempfile
import unicodedata
import urllib.parse
import urllib.request
import uuid

SOURCE = "https://raw.githubusercontent.com/oweindl/SecondBrain/main/SKILL.md"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 2 * 1024 * 1024
SECTIONS = ("Core principles", "Storage layout", "Files", "Invocation behavior",
            "Supported commands", "Command behavior", "Privacy and safety", "Implementation notes")
LIMITATIONS = [
    "Cooperating local-process locks only; noncooperating check/replace races remain.",
    "No multi-device cloud-sync atomicity or multi-file transaction.",
    "Symlinks, junctions and unknown reparse tags are refused; Windows CLOUD/CLOUD_1..F tags require no-follow handle verification.",
    "Structural validation and SHA256/HTTPS are not publisher authenticity.",
    "Host registration requires supported host tools and explicit exported readback.",
    "Windows preserves DACL/mode, not owner, SACL or named streams; encrypted targets are refused.",
    "Runtime state/backups require a user-selected private local directory.",
]


class AdapterError(Exception):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_time(value):
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AdapterError("Invalid ISO8601 timestamp") from exc
    if result.tzinfo is None:
        raise AdapterError("Timestamp must include UTC or an offset")
    return result.astimezone(dt.timezone.utc)


def timestamp(value):
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise AdapterError("Clock must return timezone-aware time")
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise AdapterError("Version must be numeric MAJOR.MINOR.PATCH")
    return tuple(map(int, value.split(".")))


def json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError("Duplicate JSON key", key=key)
        result[key] = value
    return result


def read_json(path):
    try:
        data = Path(path).read_bytes()
        if len(data) > MAX_BYTES:
            raise AdapterError("JSON exceeds size limit")
        result = json.loads(data, object_pairs_hook=json_pairs)
    except (OSError, ValueError, UnicodeError) as exc:
        raise AdapterError("Unreadable or malformed JSON", path=str(path)) from exc
    if not isinstance(result, dict):
        raise AdapterError("JSON object required", path=str(path))
    return result


def nonredirecting_cloud_tag(tag):
    return (type(tag) is int and 0 <= tag <= 0xFFFFFFFF and not tag & 0x20000000 and
            tag & 0xFFFF0FFF == 0x9000001A)


def windows_attribute_tag(path):
    """Query the final object without following its reparse point."""
    if os.name != "nt":
        raise AdapterError("Windows reparse verification unavailable", path=str(path))
    import ctypes
    from ctypes import wintypes

    class AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    create = api.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    query = api.GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    query.restype = wintypes.BOOL
    close = api.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # Zero desired access, all sharing, OPEN_EXISTING; no-follow plus directory support.
    handle = create(str(path), 0, 7, None, 3, 0x00200000 | 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise AdapterError("Cannot open reparse object for verification", path=str(path),
                           windowsError=ctypes.get_last_error())
    try:
        info = AttributeTagInfo()
        if not query(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise AdapterError("Cannot verify reparse attributes/tag", path=str(path),
                               windowsError=ctypes.get_last_error())
        return info.attributes, info.tag
    finally:
        if not close(handle):
            raise AdapterError("Reparse verification handle cleanup failed", path=str(path),
                               windowsError=ctypes.get_last_error())


def inspect_path(path):
    """Check every existing ancestor, including ancestors above the explicit root."""
    path = Path(os.path.abspath(path))
    for item in [*reversed(path.parents), path]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AdapterError("Cannot inspect path", path=str(item)) from exc
        if stat.S_ISLNK(info.st_mode):
            raise AdapterError("Symlink path refused", path=str(item))
        if getattr(info, "st_file_attributes", 0) & 0x400:
            attributes, tag = windows_attribute_tag(item)
            observed = getattr(info, "st_reparse_tag", 0)
            if (not attributes & 0x400 or not nonredirecting_cloud_tag(tag) or
                    observed not in (0, tag)):
                raise AdapterError("Redirecting/unknown/changed reparse path refused", path=str(item),
                                   reparseTag=tag)
    return path


def relative_key(key):
    if not isinstance(key, str) or not key or "\\" in key:
        raise AdapterError("Portable paths require forward-slash-separated relative keys")
    if key.startswith("/") or PureWindowsPath(key).drive:
        raise AdapterError("Absolute path refused", path=key)
    parts = key.split("/")
    for part in parts:
        if (not part or part in (".", "..") or ":" in part or
                part.endswith((" ", ".")) or any(ord(c) < 32 for c in part) or
                any(c in '<>"|?*' for c in part) or
                re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³]|conin\$|conout\$)(\..*)?", part)):
            raise AdapterError("Unsafe relative path", path=key)
    return parts


def contained(root, key):
    # CLI accepts native separators; manifest validation calls relative_key directly.
    if not isinstance(key, str):
        raise AdapterError("Relative path must be a string")
    parts = relative_key(key.replace("\\", "/"))
    root = inspect_path(root)
    if not root.is_dir():
        raise AdapterError("Root must be an existing directory", path=str(root))
    target = inspect_path(root.joinpath(*parts))
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise AdapterError("Path escapes root") from exc
    if target.exists() and not target.is_file():
        raise AdapterError("Target must be a regular file", path=str(target))
    return target


def runtime_dir(path, forbidden=(), create=True):
    if not Path(path).is_absolute():
        raise AdapterError("Runtime directory must be explicitly absolute")
    root = inspect_path(path)
    for excluded in (PACKAGE_ROOT, *forbidden):
        excluded = inspect_path(excluded)
        if root == excluded or excluded in root.parents or root in excluded.parents:
            raise AdapterError("Runtime directory must be disjoint from package/brain/install roots",
                               runtimeDir=str(root))
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    inspect_path(root)
    if not root.is_dir():
        raise AdapterError("Runtime directory is not a directory")
    return root


@contextlib.contextmanager
def exclusive_lock(path):
    path = inspect_path(path)
    handle = None
    locked = False
    try:
        handle = open(path, "a+b")
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            # Nonblocking contention is an explicit no-fetch failure, not a stale lock.
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        elif os.name == "posix":
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            raise AdapterError("No supported OS exclusive lock")
        locked = True
        yield
    except OSError as exc:
        raise AdapterError("OS lock/I/O failed", lockPath=str(path)) from exc
    finally:
        if handle is not None:
            try:
                if locked:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def revision(path):
    inspect_path(path)
    try:
        return digest(path.read_bytes())
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise AdapterError("Cannot read target", path=str(path)) from exc


def capture_protection(source):
    info = source.stat()
    if getattr(info, "st_file_attributes", 0) & 0x4000:
        raise AdapterError("Encrypted file protection cannot be preserved; target refused")
    result = {"mode": stat.S_IMODE(info.st_mode)}
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        get = api.GetFileSecurityW
        get.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        get.restype = wintypes.BOOL
        needed = wintypes.DWORD()
        get(str(source), 4, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise AdapterError("Cannot read Windows DACL")
        buffer = ctypes.create_string_buffer(needed.value)
        if not get(str(source), 4, buffer, needed, ctypes.byref(needed)):
            raise AdapterError("Cannot read Windows DACL")
        result["windowsDacl"] = base64.b64encode(buffer.raw).decode("ascii")
    else:
        result.update(uid=info.st_uid, gid=info.st_gid)
    return result


def restore_protection(metadata, destination):
    if not isinstance(metadata, dict) or type(metadata.get("mode")) is not int or not 0 <= metadata["mode"] <= 0o7777:
        raise AdapterError("Invalid protection metadata")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        try:
            raw = base64.b64decode(metadata["windowsDacl"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise AdapterError("Invalid backed-up Windows DACL") from exc
        if len(raw) < 20 or len(raw) > MAX_BYTES or raw[0] != 1 or not struct.unpack_from("<H", raw, 2)[0] & 0x8000:
            raise AdapterError("Invalid self-relative Windows security descriptor")
        for offset in struct.unpack_from("<IIII", raw, 4):
            if offset and (offset < 20 or offset >= len(raw)):
                raise AdapterError("Invalid Windows security descriptor offset")
        acl_offset = struct.unpack_from("<I", raw, 16)[0]
        if acl_offset and (acl_offset + 8 > len(raw) or
                           acl_offset + struct.unpack_from("<H", raw, acl_offset + 2)[0] > len(raw)):
            raise AdapterError("Invalid Windows ACL extent")
        buffer = ctypes.create_string_buffer(raw)
        control, rev = wintypes.WORD(), wintypes.DWORD()
        control_api = api.GetSecurityDescriptorControl
        control_api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                ctypes.POINTER(wintypes.DWORD)]
        control_api.restype = wintypes.BOOL
        if not control_api(buffer, ctypes.byref(control), ctypes.byref(rev)):
            raise AdapterError("Cannot read Windows DACL protection")
        flags = 4 | (0x80000000 if control.value & 0x1000 else 0x20000000)
        set_security = api.SetFileSecurityW
        set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        set_security.restype = wintypes.BOOL
        if not set_security(str(destination), flags, buffer):
            raise AdapterError("Cannot preserve Windows DACL")
    else:
        if any(type(metadata.get(key)) is not int or metadata[key] < 0 for key in ("uid", "gid")):
            raise AdapterError("Invalid POSIX ownership metadata")
        current = destination.stat()
        if (current.st_uid, current.st_gid) != (metadata["uid"], metadata["gid"]):
            os.chown(destination, metadata["uid"], metadata["gid"])
    os.chmod(destination, metadata["mode"])


def preserve_protection(source, destination):
    if not source.stat().st_mode & stat.S_IWUSR:
        raise AdapterError("Read-only target refused", path=str(source))
    restore_protection(capture_protection(source), destination)


def verify_protection(expected, path):
    actual = capture_protection(path)
    if actual["mode"] != expected["mode"]:
        raise AdapterError("Restored file mode mismatch", path=str(path))
    if os.name == "nt":
        def dacl_identity(value):
            raw = base64.b64decode(value, validate=True)
            if len(raw) < 20:
                raise AdapterError("Invalid Windows protection descriptor")
            control = struct.unpack_from("<H", raw, 2)[0]
            offset = struct.unpack_from("<I", raw, 16)[0]
            acl = None
            if offset:
                if offset < 20 or offset + 8 > len(raw):
                    raise AdapterError("Invalid Windows protection ACL offset")
                size = struct.unpack_from("<H", raw, offset + 2)[0]
                if size < 8 or offset + size > len(raw):
                    raise AdapterError("Invalid Windows protection ACL extent")
                acl = raw[offset:offset + size]
            # Auto-inheritance bookkeeping is not the ACL or its protected state.
            return control & (0x0004 | 0x1000), acl
        if dacl_identity(actual["windowsDacl"]) != dacl_identity(expected["windowsDacl"]):
            raise AdapterError("Restored Windows DACL/protection mismatch", path=str(path))
    elif (actual["uid"], actual["gid"]) != (expected["uid"], expected["gid"]):
        raise AdapterError("Restored POSIX ownership mismatch", path=str(path))


def write_bytes(path, data, expected, before_commit=None):
    """Caller owns a cooperating lock. Existing parents required."""
    if expected != "absent" and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise AdapterError("Expected SHA256 or literal 'absent' required")
    inspect_path(path)
    if revision(path) != expected:
        raise AdapterError("Revision conflict", path=str(path))
    stage = None
    try:
        fd, name = tempfile.mkstemp(prefix=".secondbrain-stage-", dir=path.parent)
        stage = Path(name)
        with os.fdopen(fd, "wb") as stream:
            if expected != "absent":
                preserve_protection(path, stage)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if stage.read_bytes() != data:
            raise AdapterError("Staging readback mismatch")
        if before_commit is not None:
            before_commit()
        inspect_path(path)
        if revision(path) != expected:
            raise AdapterError("Revision changed before commit", path=str(path))
        if expected == "absent":
            # Atomic create-if-absent; unsupported hardlink filesystems fail closed.
            os.link(stage, path)
        else:
            os.replace(stage, path)
        if path.read_bytes() != data:
            raise AdapterError("Committed readback mismatch", path=str(path), committed=True)
    except OSError as exc:
        raise AdapterError("Write failed", path=str(path)) from exc
    finally:
        if stage is not None and stage.exists():
            try:
                stage.unlink()
            except OSError as exc:
                raise AdapterError("Staging cleanup failed", cleanupPath=str(stage)) from exc


def save_json(path, obj):
    write_bytes(path, (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode(),
                revision(path))
    if read_json(path) != obj:
        raise AdapterError("JSON persistence readback mismatch")


def guarded_write(root, target, expected, data, runtime, approved=False, before_commit=None):
    if not approved:
        raise AdapterError("Explicit --approved required")
    runtime = runtime_dir(runtime, (root,))
    lock = runtime / ("write-" + digest(str(inspect_path(root)).casefold().encode()) + ".lock")
    with exclusive_lock(lock):
        path = contained(root, target)
        write_bytes(path, data, expected, before_commit)
    return {"success": True, "status": "written", "path": str(path), "sha256": digest(data),
            "lockPath": str(lock), "limitations": LIMITATIONS[:3]}


def skill_candidate(data, require_package_version=True):
    try:
        text = data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeError as exc:
        raise AdapterError("SKILL must be UTF-8") from exc
    match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.S)
    if not match:
        raise AdapterError("One leading package metadata block required")
    metadata = {}
    for line in match[1].splitlines():
        if not line.strip():
            continue
        field = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*", line)
        if not field or field[1] in metadata:
            raise AdapterError("Unsupported or duplicate metadata field")
        value = field[2]
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise AdapterError("Invalid quoted metadata") from exc
        elif value.startswith("'"):
            if not value.endswith("'"):
                raise AdapterError("Invalid quoted metadata")
            value = value[1:-1].replace("''", "'")
        metadata[field[1]] = value
    if metadata.get("name") != "second-brain" or not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
        raise AdapterError("Invalid SKILL name or description")
    if require_package_version or "version" in metadata:
        version(metadata.get("version"))
    body = text[match.end():]
    if body.lstrip().startswith("---"):
        raise AdapterError("Multiple leading metadata blocks refused")
    headings = set(re.findall(r"^## (.+?)\s*$", body, re.M))
    if not all(section in headings for section in SECTIONS):
        raise AdapterError("Required SKILL sections missing")
    return metadata, normalize_body(body)


def normalize_body(body):
    # Exactly CRLF -> LF and outer whitespace. Internal whitespace is semantic.
    return body.replace("\r\n", "\n").strip()


def canonical_source(source):
    if not isinstance(source, str):
        raise AdapterError("HTTPS source must be a string")
    try:
        parts = urllib.parse.urlsplit(source)
        port = parts.port
    except ValueError as exc:
        raise AdapterError("Invalid HTTPS source") from exc
    if (parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None or
            parts.password is not None or port not in (None, 443) or parts.fragment or
            parts.query or parts.hostname.lower() not in ("github.com", "raw.githubusercontent.com")):
        raise AdapterError("Credential-free canonical GitHub HTTPS source required")
    if any(c.isspace() or ord(c) < 32 for c in source):
        raise AdapterError("Invalid source characters")
    return urllib.parse.urlunsplit(("https", parts.hostname.lower(), parts.path or "/", "", ""))


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        canonical_source(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_https(source):
    opener = urllib.request.build_opener(SecureRedirect())
    with opener.open(canonical_source(source), timeout=15) as response:
        canonical_source(response.geturl())
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise AdapterError("Candidate exceeds size limit")
    return data


def update_state(path):
    if not path.exists():
        return {"schemaVersion": 1, "sources": {}}
    state = read_json(path)
    if type(state.get("schemaVersion")) is not int or state["schemaVersion"] != 1 or not isinstance(state.get("sources"), dict):
        raise AdapterError("Unsupported/malformed update state")
    for key, record in state["sources"].items():
        if canonical_source(key) != key or not isinstance(record, dict):
            raise AdapterError("Malformed source record")
        if "lastAttemptAtUtc" not in record:
            raise AdapterError("Source record lacks attempt timestamp")
        parse_time(record["lastAttemptAtUtc"])
        if record.get("lastOutcome") not in ("pending", "success", "failed", "invalid"):
            raise AdapterError("Malformed source outcome")
        if "lastSuccessfulCheckAtUtc" in record:
            parse_time(record["lastSuccessfulCheckAtUtc"])
    return state


def cached_result(runtime, record, installed):
    if "cache" not in record:
        return {"status": "cooldown_no_cache"}
    cache = record["cache"]
    if (not isinstance(cache, dict) or not isinstance(cache.get("sha256"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", cache["sha256"])):
        raise AdapterError("Malformed cached candidate")
    path = contained(runtime, cache.get("file", ""))
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AdapterError("Cached candidate unavailable") from exc
    if digest(data) != cache["sha256"]:
        raise AdapterError("Cached candidate digest mismatch")
    metadata, _ = skill_candidate(data)
    checked = parse_time(cache.get("checkedAtUtc"))
    return {"status": "cooldown_cached", "candidateVersion": metadata["version"],
            "newer": version(metadata["version"]) > version(installed),
            "checkedAtUtc": timestamp(checked), "cachePath": str(path), "sha256": digest(data),
            "validation": "structural_and_digest_only"}


def check_updates(runtime, installed, source=SOURCE, now=None, clock=None, fetch=fetch_https,
                  manual=False, opt_out=False, prompt_suppressed=False, forbidden=()):
    version(installed)
    source = canonical_source(source)
    if not manual and (opt_out or prompt_suppressed):
        return {"success": True, "status": "suppressed", "freshCheck": False}
    runtime = runtime_dir(runtime, forbidden)
    moment = now if now is not None else (clock or (lambda: dt.datetime.now(dt.timezone.utc)))()
    moment = parse_time(timestamp(moment))
    state_path = runtime / "secondbrain-update-checks.json"
    lock_path = runtime / "update-checks.lock"
    reservation = uuid.uuid4().hex
    with exclusive_lock(lock_path):
        state = update_state(state_path)
        for saved in state["sources"].values():
            times = [saved["lastAttemptAtUtc"]]
            if "lastSuccessfulCheckAtUtc" in saved:
                times.append(saved["lastSuccessfulCheckAtUtc"])
            if "cache" in saved:
                if not isinstance(saved["cache"], dict):
                    raise AdapterError("Malformed cache record; no fetch")
                times.append(saved["cache"].get("checkedAtUtc"))
            if any(parse_time(value) > moment for value in times):
                raise AdapterError("Future saved timestamp; no fetch")
        record = state["sources"].setdefault(source, {})
        if "lastAttemptAtUtc" in record:
            previous = parse_time(record["lastAttemptAtUtc"])
            if previous > moment:
                raise AdapterError("Future attempt timestamp; no fetch", nextEligibleAtUtc=timestamp(previous + dt.timedelta(hours=1)))
            if moment - previous < dt.timedelta(hours=1):
                result = cached_result(runtime, record, installed)
                return dict(result, success=True, freshCheck=False, source=source,
                            lastOutcome=record.get("lastOutcome", "unknown"),
                            nextEligibleAtUtc=timestamp(previous + dt.timedelta(hours=1)))
        record.update(lastAttemptAtUtc=timestamp(moment), lastOutcome="pending", reservation=reservation)
        save_json(state_path, state)
        if update_state(state_path)["sources"][source].get("reservation") != reservation:
            raise AdapterError("Reservation readback failed; no fetch")
    outcome = "failed"
    result = {"success": False, "status": "failed", "freshCheck": True, "source": source,
              "attemptAtUtc": timestamp(moment)}
    try:
        data = fetch(source)
        if not isinstance(data, bytes) or len(data) > MAX_BYTES:
            raise AdapterError("Transport must return bounded bytes")
        outcome = "invalid"
        metadata, _ = skill_candidate(data)
        outcome = "failed"
        cache_name = "candidate-" + reservation + ".md"
        with exclusive_lock(lock_path):
            write_bytes(runtime / cache_name, data, "absent")
            outcome = "success"
            result.update(success=True, status="checked", candidateVersion=metadata["version"],
                          newer=version(metadata["version"]) > version(installed),
                          checkedAtUtc=timestamp(moment), cachePath=str(runtime / cache_name),
                          sha256=digest(data), validation="structural_and_digest_only",
                          authenticity="not_verified")
    except (AdapterError, OSError, ValueError) as exc:
        result.update(status=outcome, error=type(exc).__name__ + ": " + str(exc)[:180])
    with exclusive_lock(lock_path):
        state = update_state(state_path)
        record = state["sources"].get(source, {})
        if record.get("reservation") == reservation:
            record["lastOutcome"] = outcome
            if outcome == "success":
                record.update(lastSuccessfulCheckAtUtc=timestamp(moment),
                              candidateVersion=metadata["version"],
                              cache={"file": cache_name, "sha256": digest(data),
                                     "checkedAtUtc": timestamp(moment)})
            else:
                record["error"] = result["error"]
            save_json(state_path, state)
        else:
            result["reservationSuperseded"] = True
        latest = parse_time(record["lastAttemptAtUtc"]) if record.get("lastAttemptAtUtc") else moment
    result["nextEligibleAtUtc"] = timestamp(latest + dt.timedelta(hours=1))
    return result


def validate_package(package):
    package = inspect_path(package)
    manifest_path = contained(package, "package-manifest.json")
    manifest = read_json(manifest_path)
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1 or manifest.get("name") != "second-brain":
        raise AdapterError("Invalid package manifest schema/name")
    version(manifest.get("version"))
    files = manifest.get("files")
    if not isinstance(files, dict) or "SKILL.md" not in files:
        raise AdapterError("Manifest must own SKILL.md")
    seen = set()
    contents = {}
    for key, value in files.items():
        relative_key(key)
        normalized = unicodedata.normalize("NFC", key).casefold()
        if normalized in seen or normalized == "package-manifest.json":
            raise AdapterError("Duplicate/reserved manifest ownership", path=key)
        seen.add(normalized)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise AdapterError("Invalid manifest SHA256", path=key)
        path = contained(package, key)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AdapterError("Owned package file unavailable", path=key) from exc
        if digest(data) != value.lower():
            raise AdapterError("Manifest hash mismatch", path=key)
        contents[key] = data
    # File/directory collisions cannot be installed, regardless of host semantics.
    for key in seen:
        if any(other.startswith(key + "/") for other in seen):
            raise AdapterError("Manifest file/directory ownership collision")
    metadata, body = skill_candidate(contents["SKILL.md"])
    if metadata["version"] != manifest["version"]:
        raise AdapterError("SKILL version does not match manifest")
    contents["package-manifest.json"] = manifest_path.read_bytes()
    return manifest, contents, body


def registration(snapshot):
    if snapshot.get("success") is not True or snapshot.get("name") != "second-brain":
        raise AdapterError("Trusted successful second-brain host snapshot required")
    if not isinstance(snapshot.get("instructions"), str):
        raise AdapterError("Host snapshot lacks instructions")
    if "enabled" in snapshot and not isinstance(snapshot["enabled"], bool):
        raise AdapterError("Host enabled state must be boolean")
    if "id" in snapshot and (not isinstance(snapshot["id"], str) or not snapshot["id"]):
        raise AdapterError("Host registration id must be a nonempty string")
    return snapshot


def registration_body(snapshot):
    text = snapshot["instructions"].replace("\r\n", "\n")
    if text.startswith("---\n"):
        return skill_candidate(text.encode(), require_package_version=False)[1]
    return normalize_body(text)


def scout_update_request(snapshot, body):
    """A request for the host to execute; Python never invokes Scout tools."""
    if not snapshot.get("id"):
        return None
    # Do not send enabled; m_update_skill preserves the existing enabled flag.
    return {"tool": "m_update_skill", "arguments": {"id": snapshot["id"], "instructions": body}}


def scout_readback_request():
    return {"tool": "m_get_skill", "arguments": {"name": "second-brain"}}


def operation_path(runtime, operation):
    if not re.fullmatch(r"[0-9a-f]{32}", operation):
        raise AdapterError("Operation ID must be 32 hex characters")
    return contained(runtime, "operations/" + operation + "/journal.json")


def load_operation(runtime, operation):
    path = operation_path(runtime, operation)
    journal = read_json(path)
    if type(journal.get("schemaVersion")) is not int or journal["schemaVersion"] != 1 or journal.get("operation") != operation:
        raise AdapterError("Invalid operation journal")
    if (journal.get("runtimeDir") != str(runtime) or
            not isinstance(journal.get("installDir"), str) or not Path(journal["installDir"]).is_absolute() or
            not isinstance(journal.get("body"), str) or not isinstance(journal.get("entries"), dict) or
            "SKILL.md" not in journal["entries"] or not isinstance(journal.get("createdDirectories"), list) or
            journal.get("stage") not in ("preparing", "prepared", "applying", "incomplete",
                                        "awaiting_registration", "registration_mismatch", "completed",
                                        "awaiting_rollback_registration", "rollback_blocked_or_failed",
                                        "rolled_back")):
        raise AdapterError("Malformed operation journal", operation=operation)
    if not isinstance(journal.get("registrationBefore"), dict):
        raise AdapterError("Missing registration backup")
    registration(journal["registrationBefore"])
    version(journal.get("candidateVersion"))
    for key, entry in journal["entries"].items():
        relative_key(key)
        if not isinstance(entry, dict) or entry.get("state") not in ("prepared", "pending", "applied", "restoring", "restored"):
            raise AdapterError("Malformed operation entry", path=key)
        for field in ("before", "after"):
            value = entry.get(field)
            if not isinstance(value, str) or (value != "absent" and not re.fullmatch(r"[0-9a-f]{64}", value)):
                raise AdapterError("Malformed operation revision", path=key)
        for field, prefix in (("backup", "before-"), ("candidate", "after-")):
            value = entry.get(field)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(prefix + r"\d+", value)):
                raise AdapterError("Malformed backup/candidate path", path=key)
        if (entry["before"] != "absent") != (entry.get("backup") is not None):
            raise AdapterError("Missing/unexpected backup reference", path=key)
        if entry["before"] != "absent" and not isinstance(entry.get("protection"), dict):
            raise AdapterError("Missing original protection metadata", path=key)
        if (entry["after"] != "absent") != (entry.get("candidate") is not None):
            raise AdapterError("Missing/unexpected staged reference", path=key)
        if entry.get("restoreBytes") not in (None, "pending", "verified"):
            raise AdapterError("Invalid byte restoration stage", path=key)
        if entry.get("restoreProtection") not in (None, "pending", "verified", "not_applicable"):
            raise AdapterError("Invalid protection restoration stage", path=key)
    for key in journal["createdDirectories"]:
        relative_key(key)
    return path, journal


@contextlib.contextmanager
def locked_operation(runtime, operation):
    _, initial = load_operation(runtime, operation)
    install = inspect_path(initial["installDir"])
    runtime_dir(runtime, (install,), create=False)
    lock = runtime / ("write-" + digest(str(install).casefold().encode()) + ".lock")
    with exclusive_lock(lock):
        journal_path, journal = load_operation(runtime, operation)
        if inspect_path(journal["installDir"]) != install:
            raise AdapterError("Operation installation location changed while acquiring lock",
                               operation=operation)
        yield journal_path, journal, install


def staged_candidate(journal_path, entry, body=None, candidate_version=None):
    data = contained(journal_path.parent, entry["candidate"]).read_bytes()
    if digest(data) != entry["after"]:
        raise AdapterError("Staged package tampered")
    if body is not None:
        metadata, actual_body = skill_candidate(data)
        if actual_body != body or metadata["version"] != candidate_version:
            raise AdapterError("Staged SKILL differs from approved body/version")
    return data


def install_prepare(runtime, package, install, snapshot, forbidden=()):
    package, install = inspect_path(package), inspect_path(install)
    if not install.is_dir():
        raise AdapterError("Install directory must already exist")
    if package == install or package in install.parents or install in package.parents:
        raise AdapterError("Source package and installation must be disjoint")
    runtime = runtime_dir(runtime, (package, install, *forbidden))
    manifest, contents, body = validate_package(package)
    snapshot = registration(snapshot)
    if snapshot.get("resourceDir") and inspect_path(snapshot["resourceDir"]) != install:
        raise AdapterError("Host resourceDir differs from install directory")
    operation = uuid.uuid4().hex
    directory = runtime / "operations" / operation
    inspect_path(directory)
    journal_path = directory / "journal.json"
    lock = runtime / ("write-" + digest(str(install).casefold().encode()) + ".lock")
    with exclusive_lock(lock):
        old_manifest_path = contained(install, "package-manifest.json")
        if old_manifest_path.exists():
            _, old_contents, _ = validate_package(install)
            previous_owned = set(old_contents)
        else:
            # Legacy instruction-only package ownership is SKILL.md, nothing else.
            previous_owned = {"SKILL.md"} if contained(install, "SKILL.md").exists() else set()
        old_skill = contained(install, "SKILL.md")
        if old_skill.exists():
            old_body = skill_candidate(old_skill.read_bytes())[1]
            if old_body != registration_body(snapshot):
                raise AdapterError("Existing disk/host registration mismatch")
        # Complete read/backup planning before creating the operation directory.
        entries = {}
        originals = {}
        for index, key in enumerate(sorted(set(contents) | previous_owned)):
            path = contained(install, key)
            before = revision(path)
            if before != "absent" and key not in previous_owned:
                raise AdapterError("Candidate collides with local-only file", path=key)
            backup = None
            mode = None
            protection = None
            if before != "absent":
                data = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
                protection = capture_protection(path)
                backup = "before-" + str(index)
                originals[key] = data
            candidate = None
            after = "absent"
            if key in contents:
                candidate = "after-" + str(index)
                after = digest(contents[key])
            entries[key] = {"before": before, "after": after, "backup": backup,
                            "candidate": candidate, "mode": mode, "protection": protection,
                            "state": "prepared"}
        journal = {"schemaVersion": 1, "operation": operation, "stage": "preparing",
                   "installDir": str(install), "runtimeDir": str(runtime),
                   "candidateVersion": manifest["version"], "body": body,
                   "registrationBefore": snapshot, "entries": entries,
                   "createdDirectories": [], "limitations": LIMITATIONS}
        directory.mkdir(parents=True, mode=0o700)
        try:
            save_json(journal_path, journal)
            for key, entry in entries.items():
                if entry["backup"]:
                    backup = directory / entry["backup"]
                    write_bytes(backup, originals[key], "absent")
                    if revision(backup) != entry["before"]:
                        raise AdapterError("Backup verification failed", path=key)
                if entry["candidate"]:
                    write_bytes(directory / entry["candidate"], contents[key], "absent")
            journal["stage"] = "prepared"
            save_json(journal_path, journal)
        except (AdapterError, OSError) as exc:
            raise AdapterError("Preparation incomplete; retained operation material", operation=operation,
                               journalPath=str(journal_path), cause=str(exc)) from exc
    return {"success": True, "status": "prepared_not_installed", "operation": operation,
            "journalPath": str(journal_path), "files": entries,
            "registrationRequest": scout_update_request(snapshot, body),
            "approvalScope": "exact staged files and registration instructions; inspect before applying"}


def guarded_delete(path, expected):
    inspect_path(path)
    if revision(path) != expected:
        raise AdapterError("Deletion revision conflict", path=str(path))
    path.unlink()
    if revision(path) != "absent":
        raise AdapterError("Deletion readback failed")


def ensure_parents(root, path, journal, journal_path):
    missing = []
    item = path.parent
    while item != root and not item.exists():
        missing.append(item)
        item = item.parent
    for item in reversed(missing):
        inspect_path(item)
        key = item.relative_to(root).as_posix()
        if key not in journal["createdDirectories"]:
            journal["createdDirectories"].append(key)
            save_json(journal_path, journal)
        item.mkdir()


def install_apply(runtime, operation, approved=False, after_step=None):
    if not approved:
        raise AdapterError("Explicit --approved required")
    runtime = runtime_dir(runtime, create=False)
    with locked_operation(runtime, operation) as (journal_path, journal, install):
        if journal["stage"] != "prepared":
            raise AdapterError("Operation is not prepared; verify or recover", operation=operation)
        try:
            for key, entry in journal["entries"].items():
                if revision(contained(install, key)) != entry["before"]:
                    raise AdapterError("Installation changed since preparation", path=key)
                if entry["candidate"]:
                    staged_candidate(journal_path, entry)
            journal["stage"] = "applying"
            save_json(journal_path, journal)
            for key, entry in journal["entries"].items():
                path = contained(install, key)
                ensure_parents(install, path, journal, journal_path)
                entry["state"] = "pending"
                save_json(journal_path, journal)
                if entry["after"] == "absent":
                    guarded_delete(path, entry["before"])
                else:
                    data = staged_candidate(journal_path, entry)
                    write_bytes(path, data, entry["before"])
                entry["state"] = "applied"
                save_json(journal_path, journal)
                if after_step:
                    after_step(key)
            journal["stage"] = "awaiting_registration"
            save_json(journal_path, journal)
        except (AdapterError, OSError) as exc:
            journal["stage"] = "incomplete"
            journal["error"] = str(exc)
            save_json(journal_path, journal)
            raise AdapterError("Installation incomplete; recover explicitly", operation=operation,
                               journalPath=str(journal_path), cause=str(exc)) from exc
    return {"success": False, "status": "awaiting_registration", "operation": operation,
            "journalPath": str(journal_path),
            "registrationRequest": scout_update_request(journal["registrationBefore"], journal["body"]),
            "readbackRequest": scout_readback_request()}


def compare_registration(readback, snapshot, expected_body):
    registration(readback)
    if registration_body(readback) != expected_body:
        raise AdapterError("Host instruction readback mismatch")
    for key in ("id", "enabled"):
        if key in snapshot and readback.get(key) != snapshot[key]:
            raise AdapterError("Host registration metadata mismatch", field=key)
    if "resourceDir" in snapshot:
        if (not isinstance(readback.get("resourceDir"), str) or
                inspect_path(readback["resourceDir"]) != inspect_path(snapshot["resourceDir"])):
            raise AdapterError("Host registration resource directory mismatch")


def install_verify(runtime, operation, readback=None, approved=False):
    runtime = runtime_dir(runtime, create=False)
    with locked_operation(runtime, operation) as (journal_path, journal, install):
        if journal["stage"] not in ("awaiting_registration", "registration_mismatch"):
            raise AdapterError("Operation not awaiting registration", operation=operation)
        if readback is None:
            return {"success": False, "status": "awaiting_registration", "operation": operation}
        try:
            compare_registration(readback, journal["registrationBefore"], journal["body"])
            for key, entry in journal["entries"].items():
                path = contained(install, key)
                current = revision(path)
                if current == entry["after"]:
                    continue
                if key == "SKILL.md" and current != "absent":
                    # A host may regenerate metadata around exactly the approved body.
                    if skill_candidate(path.read_bytes(), require_package_version=False)[1] != journal["body"]:
                        raise AdapterError("Host wrapper contains stale/different instructions")
                    if not approved:
                        raise AdapterError("Canonical wrapper restoration requires --approved")
                    entry["hostWrapperRevision"] = current
                    save_json(journal_path, journal)
                    data = staged_candidate(journal_path, entry, journal["body"],
                                            journal["candidateVersion"])
                    write_bytes(path, data, current)
                    if revision(path) != entry["after"]:
                        raise AdapterError("Canonical SKILL approved digest readback mismatch")
                else:
                    raise AdapterError("Installed bytes differ", path=key)
            validate_package(install)
            journal["stage"] = "completed"
            journal["registrationVerified"] = readback
            save_json(journal_path, journal)
        except (AdapterError, OSError) as exc:
            journal["stage"] = "registration_mismatch"
            journal["error"] = str(exc)
            save_json(journal_path, journal)
            raise AdapterError("Installation verification incomplete", operation=operation,
                               cause=str(exc), journalPath=str(journal_path)) from exc
    return {"success": True, "status": "completed", "operation": operation,
            "registrationVerified": True, "journalPath": str(journal_path)}


def recovery_plan(install, journal):
    restore_body = registration_body(journal["registrationBefore"])
    preview = []
    for key, entry in journal["entries"].items():
        current = revision(contained(install, key))
        owned = entry["state"] in ("pending", "applied", "restoring", "restored")
        accepted = {entry["after"], entry.get("hostWrapperRevision")}
        action = ("already_original" if current == entry["before"] else
                  "restore" if owned and current in accepted else "blocked")
        if (action == "blocked" and key == "SKILL.md" and current != "absent" and
                entry["state"] == "restored" and journal["stage"] in
                ("awaiting_rollback_registration", "rollback_blocked_or_failed")):
            path = contained(install, key)
            if skill_candidate(path.read_bytes(), require_package_version=False)[1] == restore_body:
                action = "restore_host_wrapper"
        if action == "already_original" and entry["before"] != "absent" and (
                entry["state"] == "restoring" or entry.get("restoreProtection") == "pending"):
            action = "restore_protection"
        preview.append({"path": key, "action": action, "currentSha256": current,
                        "restoreBytes": entry.get("restoreBytes"),
                        "restoreProtection": entry.get("restoreProtection")})
    request = scout_update_request(journal["registrationBefore"], restore_body)
    return restore_body, preview, request


def recover(runtime, operation, approved=False, readback=None):
    runtime = runtime_dir(runtime, create=False)
    with locked_operation(runtime, operation) as (journal_path, journal, install):
        if journal["stage"] == "completed":
            raise AdapterError("Completed operation is not a failed-install recovery",
                               operation=operation)
        restore_body, preview, request = recovery_plan(install, journal)
        if not approved:
            return {"success": False, "status": "recovery_preview", "operation": operation,
                    "files": preview, "registrationRestoreRequest": request, "mutated": False}
        try:
            for item in preview:
                if item["action"] == "blocked":
                    raise AdapterError("Rollback blocked by intervening edit", path=item["path"])
                if item["action"] == "restore_host_wrapper":
                    if readback is None:
                        raise AdapterError("Old host readback required to canonicalize restored wrapper")
                    compare_registration(readback, journal["registrationBefore"], restore_body)
                key = item["path"]
                entry = journal["entries"][key]
                path = contained(install, key)
                current = revision(path)
                if current != item["currentSha256"]:
                    raise AdapterError("Rollback revision changed", path=key)
                restore_bytes = current != entry["before"]
                restore_permissions = entry["before"] != "absent" and (
                    restore_bytes or entry["state"] == "restoring" or
                    entry.get("restoreProtection") == "pending")
                if restore_bytes or restore_permissions:
                    entry.update(state="restoring",
                                 restoreBytes="pending" if restore_bytes else "verified",
                                 restoreProtection="pending" if entry["before"] != "absent"
                                 else "not_applicable")
                    save_json(journal_path, journal)
                    if restore_bytes:
                        if entry["before"] == "absent":
                            guarded_delete(path, current)
                        else:
                            backup = contained(journal_path.parent, entry["backup"])
                            data = backup.read_bytes()
                            if digest(data) != entry["before"]:
                                raise AdapterError("Rollback backup digest mismatch", path=key)
                            write_bytes(path, data, current)
                        if revision(path) != entry["before"]:
                            raise AdapterError("Rollback byte readback failed", path=key)
                        entry["restoreBytes"] = "verified"
                        save_json(journal_path, journal)
                    if restore_permissions:
                        restore_protection(entry["protection"], path)
                if entry["before"] != "absent":
                    verify_protection(entry["protection"], path)
                    entry["restoreProtection"] = "verified"
                else:
                    entry["restoreProtection"] = "not_applicable"
                if revision(path) != entry["before"]:
                    raise AdapterError("Rollback readback failed", path=key)
                entry["restoreBytes"] = "verified"
                entry["state"] = "restored"
                save_json(journal_path, journal)
            journal["stage"] = "awaiting_rollback_registration"
            save_json(journal_path, journal)
            if readback is not None:
                compare_registration(readback, journal["registrationBefore"], restore_body)
                for key, entry in journal["entries"].items():
                    path = contained(install, key)
                    if revision(path) != entry["before"]:
                        raise AdapterError("Restored disk bytes changed", path=key)
                    if entry["before"] != "absent":
                        verify_protection(entry["protection"], path)
                journal["stage"] = "rolled_back"
                save_json(journal_path, journal)
        except (AdapterError, OSError) as exc:
            journal["stage"] = "rollback_blocked_or_failed"
            journal["error"] = str(exc)
            save_json(journal_path, journal)
            raise AdapterError("Rollback incomplete", operation=operation, cause=str(exc),
                               journalPath=str(journal_path),
                               registrationRestoreRequest=request) from exc
    return {"success": journal["stage"] == "rolled_back", "status": journal["stage"],
            "operation": operation, "files": preview, "journalPath": str(journal_path),
            "registrationRestoreRequest": request,
            "readbackRequest": scout_readback_request()}


def capabilities():
    return {"success": True, "status": "capabilities", "platform": sys.platform,
            "instructionOnly": {"executableGuarantees": False},
            "helperBacked": {"localOsLock": os.name in ("nt", "posix"),
                             "cooldownSeconds": 3600, "conditionalAbsentCreation": "hardlink_or_fail",
                             "replace": "same-directory os.replace plus revision recheck",
                             "protection": "Windows DACL + mode" if os.name == "nt" else "POSIX owner/group/mode",
                             "registration": "host-exported snapshots and supported tool requests only",
                             "multiFileAtomic": False, "publisherAuthenticity": False},
            "windowsCloudPlaceholders": {
                "supported": os.name == "nt", "tags": "CLOUD and CLOUD_1..F",
                "verification": "FileAttributeTagInfo on OPEN_REPARSE_POINT/BACKUP_SEMANTICS handle",
                "redirectingOrUnknownTags": "refused", "multiDeviceAtomicity": False},
            "limitations": LIMITATIONS, "backgroundPolling": False,
            "executesDownloadedCode": False, "automaticInstallation": False}


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"success": False, "status": "invalid_arguments", "error": message}))
        self.exit(2)


def main(argv=None):
    parser = JsonParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities")
    check = commands.add_parser("check-updates")
    check.add_argument("--runtime-dir", required=True)
    check.add_argument("--now", required=True, help="Authoritative ISO8601 UTC/offset timestamp")
    check.add_argument("--installed-version", required=True)
    check.add_argument("--source", default=SOURCE)
    check.add_argument("--brain-root")
    for flag in ("manual", "opt-out", "prompt-suppressed"):
        check.add_argument("--" + flag, action="store_true")
    write = commands.add_parser("guarded-write")
    for flag in ("runtime-dir", "root", "target", "expected-sha256", "content-file"):
        write.add_argument("--" + flag, required=True)
    write.add_argument("--approved", action="store_true")
    validate = commands.add_parser("validate-package")
    validate.add_argument("--package-dir", required=True)
    prepare = commands.add_parser("install-prepare")
    for flag in ("runtime-dir", "package-dir", "install-dir", "registration-snapshot"):
        prepare.add_argument("--" + flag, required=True)
    prepare.add_argument("--brain-root")
    for command in ("install-apply", "install-verify", "recover"):
        cmd = commands.add_parser(command)
        cmd.add_argument("--runtime-dir", required=True)
        cmd.add_argument("--operation", required=True)
        cmd.add_argument("--approved", action="store_true")
        if command != "install-apply":
            cmd.add_argument("--registration-readback")
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            result = capabilities()
        elif args.command == "check-updates":
            result = check_updates(args.runtime_dir, args.installed_version, args.source,
                                   now=parse_time(args.now), manual=args.manual, opt_out=args.opt_out,
                                   prompt_suppressed=args.prompt_suppressed,
                                   forbidden=(args.brain_root,) if args.brain_root else ())
        elif args.command == "guarded-write":
            result = guarded_write(args.root, args.target, args.expected_sha256,
                                   Path(args.content_file).read_bytes(), args.runtime_dir, args.approved)
        elif args.command == "validate-package":
            manifest, contents, _ = validate_package(args.package_dir)
            result = {"success": True, "status": "structurally_valid",
                      "version": manifest["version"], "ownedFiles": list(contents),
                      "authenticity": "not_verified"}
        elif args.command == "install-prepare":
            result = install_prepare(args.runtime_dir, args.package_dir, args.install_dir,
                                     read_json(args.registration_snapshot),
                                     (args.brain_root,) if args.brain_root else ())
        elif args.command == "install-apply":
            result = install_apply(args.runtime_dir, args.operation, args.approved)
        else:
            proof = read_json(args.registration_readback) if args.registration_readback else None
            if args.command == "install-verify":
                result = install_verify(args.runtime_dir, args.operation, proof, args.approved)
            else:
                result = recover(args.runtime_dir, args.operation, args.approved, proof)
    except (AdapterError, OSError, ValueError) as exc:
        result = {"success": False, "status": "blocked_or_failed", "error": str(exc)}
        if hasattr(args, "operation"):
            result["operation"] = args.operation
            if re.fullmatch(r"[0-9a-f]{32}", args.operation):
                result["journalPath"] = str(Path(args.runtime_dir) / "operations" / args.operation / "journal.json")
        if isinstance(exc, AdapterError):
            result.update(exc.details)
    print(json.dumps(result))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
