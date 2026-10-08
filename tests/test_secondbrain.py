"""Disposable fixtures only; no network, host tools, personal state or dependencies."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "secondbrain.py"
spec = importlib.util.spec_from_file_location("secondbrain", MODULE_PATH)
sb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sb)
NOW = sb.parse_time("2026-10-08T11:00:00Z")


def skill(ver="0.6.0", note=None):
    note = note if note is not None else "Approved instructions " + ver
    return ('---\nname: "second-brain"\nversion: "' + ver +
            '"\ndescription: "Fixture only"\n---\n# SecondBrain\n' +
            note + "\n" + "".join("\n## " + s + "\nContent\n" for s in sb.SECTIONS)).encode()


def concurrent_check(runtime, counter, queue):
    def fetch(_):
        with counter.get_lock():
            counter.value += 1
        return skill()
    try:
        result = sb.check_updates(runtime, "0.5.1", now=NOW, fetch=fetch)
        queue.put(result["status"])
    except sb.AdapterError:
        queue.put("lock_blocked")


class Fixture(unittest.TestCase):
    def setUp(self):
        # Explicit project-adjacent disposable fixtures, not OS temporary directories.
        self.temp = tempfile.TemporaryDirectory(prefix="secondbrain-fixture-",
                                                dir=MODULE_PATH.parents[2])
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = self.base / "runtime"
        self.root = self.base / "brain"
        self.root.mkdir()

    def state(self):
        return sb.read_json(self.runtime / "secondbrain-update-checks.json")

    def persist_state(self, obj):
        (self.runtime / "secondbrain-update-checks.json").write_text(json.dumps(obj), encoding="utf-8")

    def check(self, seconds=0, **kwargs):
        return sb.check_updates(self.runtime, "0.5.1", now=NOW + dt.timedelta(seconds=seconds),
                                fetch=kwargs.pop("fetch", lambda _: skill()), **kwargs)

    def package(self, name="package", ver="0.6.0", extras=None):
        root = self.base / name
        root.mkdir()
        contents = {"SKILL.md": skill(ver), **(extras or {})}
        for key, data in contents.items():
            path = root.joinpath(*key.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        manifest = {"schemaVersion": 1, "name": "second-brain", "version": ver,
                    "files": {key: sb.digest(data) for key, data in contents.items()}}
        (root / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def setup_install(self, extras=None, old_extras=None, legacy=False, before_prepare=None):
        self.package_root = self.package(extras=extras)
        self.install = self.package("installed", "0.5.1", old_extras)
        if legacy:
            (self.install / "package-manifest.json").unlink()
        if before_prepare:
            before_prepare(self.install)
        self.snapshot = {"success": True, "name": "second-brain", "id": "fixture-id",
                         "enabled": False, "instructions": sb.skill_candidate(skill("0.5.1"))[1],
                         "resourceDir": str(self.install)}
        prepared = sb.install_prepare(self.runtime, self.package_root, self.install, self.snapshot)
        self.operation = prepared["operation"]
        self.journal = Path(prepared["journalPath"])
        return prepared

    def proof(self, **kwargs):
        return {**self.snapshot, "instructions": sb.skill_candidate(skill())[1], **kwargs}


class CooldownTests(Fixture):
    def test_boundaries_restart_offsets_and_version_recomparison(self):
        calls = []
        first = self.check(fetch=lambda url: calls.append(url) or skill())
        self.assertTrue(first["newer"])
        self.assertEqual(self.check(3599)["status"], "cooldown_cached")
        self.assertEqual(self.check(3600)["status"], "checked")
        self.assertEqual(self.check(3660)["status"], "cooldown_cached")
        result = sb.check_updates(self.runtime, "0.7.0", now=sb.parse_time("2026-10-08T14:00:00+02:00"),
                                 fetch=lambda _: self.fail("network during restart cooldown"))
        self.assertFalse(result["newer"])
        self.assertEqual(len(calls), 1)

    def test_sixty_one_minutes_is_due(self):
        self.check()
        self.assertEqual(self.check(3660)["status"], "checked")

    def test_injectable_clock(self):
        result = sb.check_updates(self.runtime, "0.5.1", clock=lambda: NOW, fetch=lambda _: skill())
        self.assertEqual(result["attemptAtUtc"], "2026-10-08T11:00:00Z")

    def test_failed_request_consumes_interval_and_preserves_cache(self):
        self.check()
        def fail(_):
            raise OSError("fixture transport failure")
        result = self.check(3600, fetch=fail)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        cached = self.check(3601)
        self.assertEqual(cached["checkedAtUtc"], sb.timestamp(NOW))
        self.assertEqual(cached["lastOutcome"], "failed")

    def test_invalid_consumes_interval_without_false_currency(self):
        result = self.check(fetch=lambda _: b"not a skill")
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["success"])
        self.assertEqual(self.check(10)["status"], "cooldown_no_cache")

    def test_crash_after_pending_reservation_consumes_interval(self):
        def crash(_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.check(fetch=crash)
        self.assertEqual(self.state()["sources"][sb.SOURCE]["lastOutcome"], "pending")
        self.assertEqual(self.check(3599)["status"], "cooldown_no_cache")
        self.assertEqual(self.check(3600)["status"], "checked")

    def test_unknown_fields_preserved(self):
        self.check()
        state = self.state()
        state["custom"] = {"keep": 1}
        state["sources"][sb.SOURCE]["unknown"] = "keep"
        self.persist_state(state)
        self.check(3600)
        state = self.state()
        self.assertEqual(state["custom"], {"keep": 1})
        self.assertEqual(state["sources"][sb.SOURCE]["unknown"], "keep")

    def test_corrupt_unsupported_unreadable_and_future_states_never_fetch(self):
        self.runtime.mkdir()
        path = self.runtime / "secondbrain-update-checks.json"
        for text in ("{", '{"schemaVersion":2,"sources":{}}',
                     '{"schemaVersion":1,"sources":{"x":{}}}',
                     '{"schemaVersion":1,"sources":[]}'):
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(sb.AdapterError):
                self.check(fetch=lambda _: self.fail("fetch on corrupt state"))
        path.unlink()
        path.mkdir()
        with self.assertRaises(sb.AdapterError):
            self.check(fetch=lambda _: self.fail("fetch on unreadable state"))
        path.rmdir()
        self.check()
        with self.assertRaises(sb.AdapterError):
            self.check(-1, fetch=lambda _: self.fail("fetch on future timestamp"))

    def test_cache_tampering_blocks_cooldown_reuse(self):
        result = self.check()
        Path(result["cachePath"]).write_bytes(skill(note="Tampered"))
        with self.assertRaises(sb.AdapterError):
            self.check(1)

    def test_future_cache_and_success_timestamps_block_fetch(self):
        self.check()
        state = self.state()
        record = state["sources"][sb.SOURCE]
        record["lastSuccessfulCheckAtUtc"] = sb.timestamp(NOW + dt.timedelta(days=1))
        self.persist_state(state)
        with self.assertRaises(sb.AdapterError):
            self.check(3600, fetch=lambda _: self.fail("future success timestamp"))
        record["lastSuccessfulCheckAtUtc"] = sb.timestamp(NOW)
        record["cache"]["checkedAtUtc"] = sb.timestamp(NOW + dt.timedelta(days=1))
        self.persist_state(state)
        with self.assertRaises(sb.AdapterError):
            self.check(3600, fetch=lambda _: self.fail("future cache timestamp"))

    def test_lock_or_reservation_persistence_failure_never_fetch(self):
        @contextlib.contextmanager
        def unavailable(_):
            raise sb.AdapterError("fixture locking unavailable")
            yield
        with patch.object(sb, "exclusive_lock", unavailable):
            with self.assertRaises(sb.AdapterError):
                self.check(fetch=lambda _: self.fail("fetch without lock"))
        with patch.object(sb, "save_json", side_effect=sb.AdapterError("fixture write failed")):
            with self.assertRaises(sb.AdapterError):
                self.check(fetch=lambda _: self.fail("fetch without reservation"))

    def test_readonly_state_blocks_due_fetch(self):
        self.check()
        path = self.runtime / "secondbrain-update-checks.json"
        path.chmod(stat.S_IREAD)
        self.addCleanup(lambda: path.chmod(stat.S_IREAD | stat.S_IWRITE))
        with self.assertRaises(sb.AdapterError):
            self.check(3600, fetch=lambda _: self.fail("fetch on readonly persistence"))

    def test_malformed_cache_is_explicit_failure(self):
        self.check()
        state = self.state()
        state["sources"][sb.SOURCE]["cache"]["sha256"] = None
        self.persist_state(state)
        with self.assertRaises(sb.AdapterError):
            self.check(1)

    def test_candidate_cache_write_failure_is_not_verified(self):
        real = sb.write_bytes
        def failure(path, *args, **kwargs):
            if path.name.startswith("candidate-"):
                raise sb.AdapterError("fixture cache storage failure")
            return real(path, *args, **kwargs)
        with patch.object(sb, "write_bytes", failure):
            result = self.check()
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("cache", self.state()["sources"][sb.SOURCE])

    def test_manual_bypasses_preferences_not_cooldown(self):
        self.assertEqual(self.check(opt_out=True)["status"], "suppressed")
        self.assertFalse(self.runtime.exists())
        self.check(manual=True, opt_out=True, prompt_suppressed=True)
        self.assertEqual(self.check(3599, manual=True, opt_out=True)["status"], "cooldown_cached")

    def test_future_reservation_not_clobbered_by_old_finisher(self):
        newer = []
        def late(_):
            newer.append(self.check(3600))
            return skill("0.6.1")
        older = self.check(fetch=late)
        self.assertTrue(older["reservationSuperseded"])
        record = self.state()["sources"][sb.SOURCE]
        self.assertEqual(record["candidateVersion"], "0.6.0")
        self.assertEqual(record["lastAttemptAtUtc"], sb.timestamp(NOW + dt.timedelta(hours=1)))

    def test_simultaneous_processes_issue_one_fetch(self):
        context = multiprocessing.get_context("spawn")
        counter = context.Value("i", 0)
        queue = context.Queue()
        self.addCleanup(queue.close)
        processes = [context.Process(target=concurrent_check,
                                     args=(str(self.runtime), counter, queue)) for _ in range(6)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join()
                self.fail("Child process did not finish")
            self.assertEqual(process.exitcode, 0)
        statuses = [queue.get(timeout=5) for _ in processes]
        self.assertEqual(counter.value, 1, statuses)
        self.assertEqual(statuses.count("checked"), 1, statuses)

    def test_source_credentials_insecure_urls_and_redirects_rejected(self):
        self.assertEqual(sb.canonical_source("https://RAW.GITHUBUSERCONTENT.COM:443/a"),
                         "https://raw.githubusercontent.com/a")
        for source in ("http://github.com/a", "https://user:pass@github.com/a",
                       "https://github.com:444/a", "https://example.com/a",
                       "https://github.com/a#b", "https://github.com/a?b"):
            with self.assertRaises(sb.AdapterError):
                self.check(source=source)
        with self.assertRaises(sb.AdapterError):
            sb.SecureRedirect().redirect_request(None, None, 302, "", {}, "http://github.com/a")

    def test_bounded_candidate(self):
        self.assertFalse(self.check(fetch=lambda _: b"a" * (sb.MAX_BYTES + 1))["success"])

    def test_https_transport_has_explicit_socket_timeout_and_bounded_read(self):
        from unittest.mock import MagicMock
        opener, response = MagicMock(), MagicMock()
        response.geturl.return_value = sb.SOURCE
        response.read.return_value = skill()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(sb.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(sb.fetch_https(sb.SOURCE), skill())
            opener.open.assert_called_once_with(sb.SOURCE, timeout=15)
            response.read.assert_called_once_with(sb.MAX_BYTES + 1)
            response.read.return_value = b"x" * (sb.MAX_BYTES + 1)
            with self.assertRaises(sb.AdapterError):
                sb.fetch_https(sb.SOURCE)

    def test_runtime_inside_package_or_brain_is_refused(self):
        for runtime in (self.root / "runtime", self.package("source") / "runtime"):
            with self.assertRaises(sb.AdapterError):
                sb.check_updates(runtime, "0.5.1", now=NOW, fetch=lambda _: self.fail("fetch"),
                                 forbidden=(self.root, runtime.parent))


class WriteTests(Fixture):
    def write(self, expected, data=b"new", **kwargs):
        return sb.guarded_write(self.root, "context.md", expected, data, self.runtime,
                                approved=kwargs.pop("approved", True), **kwargs)

    def test_create_and_replace_with_readback(self):
        result = self.write("absent")
        self.assertEqual(result["sha256"], sb.digest(b"new"))
        self.write(sb.digest(b"new"), b"final")
        self.assertEqual((self.root / "context.md").read_bytes(), b"final")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["context.md"])

    def test_approval_and_expected_revision_required(self):
        with self.assertRaises(sb.AdapterError):
            self.write("absent", approved=False)
        self.assertFalse((self.root / "context.md").exists())
        with self.assertRaises(sb.AdapterError):
            self.write("no hash")

    def test_external_revision_conflict(self):
        (self.root / "context.md").write_bytes(b"external")
        with self.assertRaises(sb.AdapterError):
            self.write(sb.digest(b"old"))
        self.assertEqual((self.root / "context.md").read_bytes(), b"external")

    def test_mutation_before_replace_is_preserved(self):
        path = self.root / "context.md"
        path.write_bytes(b"original")
        with self.assertRaises(sb.AdapterError):
            self.write(sb.digest(b"original"), before_commit=lambda: path.write_bytes(b"external"))
        self.assertEqual(path.read_bytes(), b"external")
        self.assertFalse(list(self.root.glob(".secondbrain-stage-*")))

    def test_appearing_absent_target_preserved(self):
        path = self.root / "context.md"
        with self.assertRaises(sb.AdapterError):
            self.write("absent", before_commit=lambda: path.write_bytes(b"appeared"))
        self.assertEqual(path.read_bytes(), b"appeared")

    def test_absent_commit_race_uses_atomic_create(self):
        from unittest.mock import patch
        real_link = os.link
        path = self.root / "context.md"
        def appearing_link(src, dst):
            path.write_bytes(b"external")
            return real_link(src, dst)
        with patch.object(sb.os, "link", appearing_link):
            with self.assertRaises(sb.AdapterError):
                self.write("absent")
        self.assertEqual(path.read_bytes(), b"external")

    def test_read_only_preserved_and_failure_reported(self):
        path = self.root / "context.md"
        path.write_bytes(b"private")
        path.chmod(stat.S_IREAD)
        self.addCleanup(lambda: path.chmod(stat.S_IREAD | stat.S_IWRITE) if path.exists() else None)
        with self.assertRaises(sb.AdapterError):
            self.write(sb.digest(b"private"))
        self.assertEqual(path.read_bytes(), b"private")

    def test_missing_parent_is_not_silently_created(self):
        with self.assertRaises(sb.AdapterError):
            sb.guarded_write(self.root, "missing/context.md", "absent", b"x", self.runtime, True)
        self.assertFalse((self.root / "missing").exists())

    def test_invalid_paths(self):
        for key in ("../outside", "/absolute", "C:\\absolute", "a:stream", "a/../x",
                    "a//x", "CON.txt", "trailing.", "trailing ", "\\\\server\\share\\file"):
            with self.assertRaises(sb.AdapterError, msg=key):
                sb.guarded_write(self.root, key, "absent", b"x", self.runtime, True)

    def test_symlink_parent_and_final_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "data").write_bytes(b"outside")
        try:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
            (self.root / "context.md").symlink_to(outside / "data")
        except OSError:
            self.skipTest("Host does not permit creating symbolic links")
        for key in ("link/new", "context.md"):
            with self.assertRaises(sb.AdapterError):
                sb.guarded_write(self.root, key, "absent", b"x", self.runtime, True)
        self.assertEqual((outside / "data").read_bytes(), b"outside")

    def test_permissions_preserved(self):
        self.write("absent")
        path = self.root / "context.md"
        path.chmod(0o600)
        before = stat.S_IMODE(path.stat().st_mode)
        self.write(sb.digest(b"new"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), before)

    def test_failed_replace_leaves_old_bytes(self):
        path = self.root / "context.md"
        path.write_bytes(b"old")
        with patch.object(sb.os, "replace", side_effect=PermissionError("fixture replace denied")):
            with self.assertRaises(sb.AdapterError):
                self.write(sb.digest(b"old"))
        self.assertEqual(path.read_bytes(), b"old")

    def test_staging_cleanup_failure_reported(self):
        real = Path.unlink
        def fail_stage(path, *args, **kwargs):
            if path.name.startswith(".secondbrain-stage-"):
                raise PermissionError("fixture cleanup failure")
            return real(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_stage):
            with self.assertRaises(sb.AdapterError) as error:
                self.write("absent")
        self.assertIn("cleanupPath", error.exception.details)
        self.assertEqual((self.root / "context.md").read_bytes(), b"new")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_junction_escape_is_refused(self):
        import subprocess
        outside = self.base / "outside"
        outside.mkdir()
        junction = self.root / "redirect"
        result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/c", "mklink", "/J",
                                 str(junction), str(outside)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(sb.AdapterError):
            sb.guarded_write(self.root, "redirect/data", "absent", b"x", self.runtime, True)
        self.assertFalse((outside / "data").exists())

    def test_unverified_lstat_reparse_claims_are_refused(self):
        import types
        real = Path.lstat
        real_query = sb.windows_attribute_tag
        path = self.root / "context.md"
        path.write_bytes(b"old")
        for tag in (0xA0000003, 0x9000001A, 0x9000101A):
            def fake(item, *args, **kwargs):
                if item == path:
                    return types.SimpleNamespace(st_mode=stat.S_IFREG,
                                                 st_file_attributes=0x400, st_reparse_tag=tag)
                return real(item, *args, **kwargs)
            with patch.object(Path, "lstat", fake), patch.object(
                    sb, "windows_attribute_tag",
                    side_effect=lambda item: (0x20, 0) if item == path else real_query(item)):
                with self.assertRaises(sb.AdapterError):
                    self.write(sb.digest(b"old"))

    def test_cloud_tag_classifier_accepts_only_known_nonredirecting_family(self):
        for number in range(16):
            self.assertTrue(sb.nonredirecting_cloud_tag(0x9000001A | (number << 12)))
        for tag in (0, True, None, "0x9000001A", -1, 0x19000001A,
                    0xA0000003, 0xA000000C, 0xB000001A, 0x9000001B, 0x9001001A):
            self.assertFalse(sb.nonredirecting_cloud_tag(tag), tag)

    def test_known_cloud_tags_require_consistent_handle_verified_attributes(self):
        import types
        real = Path.lstat
        real_query = sb.windows_attribute_tag
        path = self.root / "context.md"
        path.write_bytes(b"local fixture")
        for number in range(16):
            tag = 0x9000001A | (number << 12)
            def fake(item, *args, **kwargs):
                if item == path:
                    return types.SimpleNamespace(st_mode=stat.S_IFREG,
                                                 st_file_attributes=0x400, st_reparse_tag=tag)
                return real(item, *args, **kwargs)
            with patch.object(Path, "lstat", fake):
                with patch.object(sb, "windows_attribute_tag",
                                  side_effect=lambda item: (0x400, tag) if item == path else real_query(item)) as query:
                    self.assertEqual(sb.inspect_path(path), path)
                    query.assert_any_call(path)
                for attributes, verified_tag in ((0x20, tag), (0x400, 0xA0000003),
                                                  (0x400, 0x9000001B), (0x400, tag ^ 0x1000)):
                    with patch.object(sb, "windows_attribute_tag",
                                      side_effect=lambda item: (attributes, verified_tag)
                                      if item == path else real_query(item)):
                        with self.assertRaises(sb.AdapterError):
                            sb.inspect_path(path)
                with patch.object(sb, "windows_attribute_tag",
                                  side_effect=sb.AdapterError("fixture no-follow query failed")):
                    with self.assertRaises(sb.AdapterError):
                        sb.inspect_path(path)

    @unittest.skipUnless(os.name == "nt", "Windows attribute-tag API test")
    def test_actual_windows_attribute_tag_query_for_materialized_fixtures(self):
        path = self.root / "context.md"
        path.write_bytes(b"materialized fixture")
        for item in (self.root, path):
            attributes, tag = sb.windows_attribute_tag(item)
            self.assertEqual(attributes & 0x400, item.lstat().st_file_attributes & 0x400)
            if attributes & 0x400:
                self.assertTrue(sb.nonredirecting_cloud_tag(tag))
            else:
                self.assertEqual(tag, 0)

    @unittest.skipUnless(os.name == "nt", "Windows no-follow API contract test")
    def test_windows_tag_query_uses_no_follow_handle_and_closes_on_failure(self):
        import ctypes
        from ctypes import wintypes
        from unittest.mock import Mock
        def fill(handle, kind, pointer, size):
            values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
            values[0], values[1] = 0x400, 0x9000001A
            return True
        api = Mock()
        api.CreateFileW.return_value = 123
        api.GetFileInformationByHandleEx.side_effect = fill
        api.CloseHandle.return_value = True
        with patch.object(ctypes, "WinDLL", return_value=api):
            self.assertEqual(sb.windows_attribute_tag(self.root), (0x400, 0x9000001A))
            args = api.CreateFileW.call_args.args
            self.assertEqual(args[1:3], (0, 7))
            self.assertEqual(args[4:6], (3, 0x00200000 | 0x02000000))
            self.assertEqual(api.GetFileInformationByHandleEx.call_args.args[1], 9)
            api.CloseHandle.assert_called_once_with(123)
            api.GetFileInformationByHandleEx.side_effect = None
            api.GetFileInformationByHandleEx.return_value = False
            with self.assertRaises(sb.AdapterError):
                sb.windows_attribute_tag(self.root)
            self.assertEqual(api.CloseHandle.call_count, 2)
            api.GetFileInformationByHandleEx.side_effect = fill
            api.CloseHandle.return_value = False
            with self.assertRaises(sb.AdapterError):
                sb.windows_attribute_tag(self.root)


class PackageTests(Fixture):
    def test_manifest_validates_hashes_and_skips_unknown_extras(self):
        package = self.package(extras={"scripts/secondbrain.py": b"# fixture code"})
        (package / "unknown.txt").write_bytes(b"local-only")
        _, files, _ = sb.validate_package(package)
        self.assertEqual(set(files), {"SKILL.md", "scripts/secondbrain.py", "package-manifest.json"})
        (package / "SKILL.md").write_bytes(skill(note="changed"))
        with self.assertRaises(sb.AdapterError):
            sb.validate_package(package)

    def test_manifest_path_hash_schema_version_errors(self):
        package = self.package()
        path = package / "package-manifest.json"
        original = sb.read_json(path)
        for key, value in (("../outside", "a" * 64), ("/absolute", "a" * 64),
                           ("C:/absolute", "a" * 64), ("a:stream", "a" * 64),
                           ("scripts\\a.py", "a" * 64), ("SKILL.md", "bad"),
                           ("skill.md", "a" * 64), ("package-manifest.json", "a" * 64)):
            mutated = {**original, "files": {**original["files"], key: value}}
            path.write_text(json.dumps(mutated), encoding="utf-8")
            with self.assertRaises(sb.AdapterError, msg=key):
                sb.validate_package(package)
        for field, value in (("schemaVersion", 2), ("version", "0.7.0"), ("name", "other")):
            path.write_text(json.dumps({**original, field: value}), encoding="utf-8")
            with self.assertRaises(sb.AdapterError):
                sb.validate_package(package)
        path.write_text('{"schemaVersion":1,"schemaVersion":1}', encoding="utf-8")
        with self.assertRaises(sb.AdapterError):
            sb.validate_package(package)

    def test_skill_structure_and_semver(self):
        for data in (b"---\nname: second-brain\n---\nx", skill().replace(b"0.6.0", b"0.6"),
                     skill().replace(b"Fixture only", b""),
                     skill().replace(b"## Files", b"## Removed"),
                     skill().replace(b"# SecondBrain", b"---\nname: second-brain\n---")):
            with self.assertRaises(sb.AdapterError):
                sb.skill_candidate(data)

    def test_instruction_normalization_preserves_semantics(self):
        body = sb.skill_candidate(skill())[1]
        self.assertEqual(sb.registration_body({"instructions": "\r\n" + body.replace("\n", "\r\n") + "\r\n"}),
                         body)
        self.assertNotEqual(sb.registration_body({"instructions": body.replace("Content", " Content")}), body)

    def test_manifest_link_refused(self):
        package = self.package()
        original = package / "SKILL.md"
        original.unlink()
        external = self.base / "external.md"
        external.write_bytes(skill())
        try:
            original.symlink_to(external)
        except OSError:
            self.skipTest("Host does not permit creating symbolic links")
        with self.assertRaises(sb.AdapterError):
            sb.validate_package(package)


class InstallationTests(Fixture):
    @contextlib.contextmanager
    def acquiring_after(self, action):
        real_lock = sb.exclusive_lock
        triggered = False
        @contextlib.contextmanager
        def delayed_lock(path):
            nonlocal triggered
            if not triggered:
                triggered = True
                with patch.object(sb, "exclusive_lock", real_lock):
                    action()
            with real_lock(path):
                yield
        with patch.object(sb, "exclusive_lock", delayed_lock):
            yield

    def test_recovery_reloads_completed_stage_after_lock_acquisition(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        completed = []
        def verify_first():
            self.assertTrue(sb.install_verify(self.runtime, self.operation, self.proof())["success"])
            completed.append(self.journal.read_bytes())
        with self.acquiring_after(verify_first):
            with self.assertRaises(sb.AdapterError):
                sb.recover(self.runtime, self.operation, True, self.snapshot)
        self.assertEqual(self.journal.read_bytes(), completed[0])
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill())
        self.assertEqual(sb.read_json(self.journal)["stage"], "completed")

    def test_apply_reloads_stage_after_competing_apply(self):
        self.setup_install()
        applied = []
        def apply_first():
            sb.install_apply(self.runtime, self.operation, True)
            applied.append(self.journal.read_bytes())
        with self.acquiring_after(apply_first):
            with self.assertRaises(sb.AdapterError):
                sb.install_apply(self.runtime, self.operation, True)
        self.assertEqual(self.journal.read_bytes(), applied[0])
        self.assertEqual(sb.read_json(self.journal)["stage"], "awaiting_registration")
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill())

    def test_verify_reloads_stage_after_competing_rollback(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        rolled_back = []
        def rollback_first():
            self.assertTrue(sb.recover(self.runtime, self.operation, True, self.snapshot)["success"])
            rolled_back.append(self.journal.read_bytes())
        with self.acquiring_after(rollback_first):
            with self.assertRaises(sb.AdapterError):
                sb.install_verify(self.runtime, self.operation, self.proof(), True)
        self.assertEqual(self.journal.read_bytes(), rolled_back[0])
        self.assertEqual(sb.read_json(self.journal)["stage"], "rolled_back")
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill("0.5.1"))

    def test_operation_location_change_during_lock_acquisition_is_refused(self):
        self.setup_install()
        def change_location():
            journal = sb.read_json(self.journal)
            journal["installDir"] = str(self.root)
            sb.save_json(self.journal, journal)
        with self.acquiring_after(change_location):
            with self.assertRaises(sb.AdapterError):
                sb.install_apply(self.runtime, self.operation, True)
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill("0.5.1"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_recreated_removed_file_retries_and_verifies_protection_after_failure(self):
        def set_original_protection(install):
            path = install / "removed.py"
            if os.name == "nt":
                metadata = sb.capture_protection(path)
                raw = bytearray(sb.base64.b64decode(metadata["windowsDacl"]))
                control = sb.struct.unpack_from("<H", raw, 2)[0] | 0x1000
                sb.struct.pack_into("<H", raw, 2, control)
                metadata["windowsDacl"] = sb.base64.b64encode(raw).decode()
                sb.restore_protection(metadata, path)
                captured = sb.capture_protection(path)
                self.assertTrue(sb.struct.unpack_from("<H",
                                sb.base64.b64decode(captured["windowsDacl"]), 2)[0] & 0x1000)
            else:
                path.chmod(0o640)
        self.setup_install(old_extras={"removed.py": b"original owned file"},
                           before_prepare=set_original_protection)
        path = self.install / "removed.py"
        expected = sb.read_json(self.journal)["entries"]["removed.py"]["protection"]
        sb.install_apply(self.runtime, self.operation, True)
        self.assertFalse(path.exists())
        real_restore = sb.restore_protection
        def fail_removed(metadata, target):
            if target == path:
                raise sb.AdapterError("fixture protection restoration failed")
            return real_restore(metadata, target)
        for _ in range(2):
            with patch.object(sb, "restore_protection", side_effect=fail_removed) as restore:
                with self.assertRaises(sb.AdapterError):
                    sb.recover(self.runtime, self.operation, True, self.snapshot)
                restore.assert_any_call(expected, path)
            self.assertEqual(path.read_bytes(), b"original owned file")
            entry = sb.read_json(self.journal)["entries"]["removed.py"]
            self.assertEqual(entry["restoreBytes"], "verified")
            self.assertEqual(entry["restoreProtection"], "pending")
            self.assertEqual(entry["state"], "restoring")
            self.assertNotEqual(sb.read_json(self.journal)["stage"], "rolled_back")
            with self.assertRaises(sb.AdapterError):
                sb.verify_protection(expected, path)
        with patch.object(sb, "restore_protection", wraps=real_restore) as restore, patch.object(
                sb, "verify_protection", wraps=sb.verify_protection) as verify:
            self.assertTrue(sb.recover(self.runtime, self.operation, True, self.snapshot)["success"])
            restore.assert_any_call(expected, path)
            verify.assert_any_call(expected, path)
        self.assertEqual(sb.read_json(self.journal)["entries"]["removed.py"]["restoreProtection"], "verified")
        sb.verify_protection(expected, path)

    def test_interrupted_protection_restoration_remains_pending_and_retries(self):
        self.setup_install(old_extras={"removed.py": b"original owned file"})
        path = self.install / "removed.py"
        expected = sb.read_json(self.journal)["entries"]["removed.py"]["protection"]
        sb.install_apply(self.runtime, self.operation, True)
        real_restore = sb.restore_protection
        def interrupt(metadata, target):
            if target == path:
                raise KeyboardInterrupt()
            return real_restore(metadata, target)
        with patch.object(sb, "restore_protection", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                sb.recover(self.runtime, self.operation, True)
        self.assertEqual(path.read_bytes(), b"original owned file")
        self.assertEqual(sb.read_json(self.journal)["entries"]["removed.py"]["restoreProtection"], "pending")
        with patch.object(sb, "restore_protection", wraps=real_restore) as restore:
            self.assertTrue(sb.recover(self.runtime, self.operation, True, self.snapshot)["success"])
            restore.assert_any_call(expected, path)

    def test_post_apply_staging_tamper_does_not_overwrite_matching_host_wrapper(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        path = self.install / "SKILL.md"
        wrapper = skill().replace(b'version: "0.6.0"\n', b"", 1)
        path.write_bytes(wrapper)
        journal = sb.read_json(self.journal)
        candidate = self.journal.parent / journal["entries"]["SKILL.md"]["candidate"]
        candidate.write_bytes(skill(note="corrupted staging after apply"))
        with self.assertRaises(sb.AdapterError):
            sb.install_verify(self.runtime, self.operation, self.proof(), True)
        self.assertEqual(path.read_bytes(), wrapper)
        self.assertEqual(sb.read_json(self.journal)["stage"], "registration_mismatch")
        self.assertTrue(sb.recover(self.runtime, self.operation, True, self.snapshot)["success"])
        self.assertEqual(path.read_bytes(), skill("0.5.1"))

    def test_recovery_preview_never_initializes_missing_runtime(self):
        with self.assertRaises(sb.AdapterError):
            sb.recover(self.runtime, "0" * 32)
        self.assertFalse(self.runtime.exists())

    def test_complete_supported_host_bridge_and_disabled_state(self):
        prepared = self.setup_install(extras={"scripts/secondbrain.py": b"# fixture"})
        (self.install / "custom.txt").write_bytes(b"local only")
        (self.package_root / "unknown.txt").write_bytes(b"do not copy")
        request = prepared["registrationRequest"]
        self.assertEqual(request, {"tool": "m_update_skill",
                                  "arguments": {"id": "fixture-id", "instructions": self.proof()["instructions"]}})
        self.assertNotIn("enabled", request["arguments"])
        applied = sb.install_apply(self.runtime, self.operation, True)
        self.assertFalse(applied["success"])
        self.assertEqual(applied["status"], "awaiting_registration")
        self.assertEqual((self.install / "custom.txt").read_bytes(), b"local only")
        self.assertFalse((self.install / "unknown.txt").exists())
        self.assertFalse(sb.install_verify(self.runtime, self.operation)["success"])
        self.assertTrue(sb.install_verify(self.runtime, self.operation, self.proof())["success"])
        self.assertEqual(sb.read_json(self.journal)["stage"], "completed")

    def test_no_stable_host_id_reports_limited_bridge(self):
        self.setup_install()
        # Repeat with a snapshot without stable id, as some host exports omit it.
        snapshot = {k: v for k, v in self.snapshot.items() if k != "id"}
        prepared = sb.install_prepare(self.runtime, self.package_root, self.install, snapshot)
        self.assertIsNone(prepared["registrationRequest"])
        applied = sb.install_apply(self.runtime, prepared["operation"], True)
        self.assertFalse(applied["success"])
        self.assertIsNone(applied["registrationRequest"])

    def test_prior_owned_removed_only_is_pruned_and_backed_up(self):
        self.setup_install(old_extras={"old.py": b"owned old"})
        (self.install / "custom.py").write_bytes(b"local custom")
        sb.install_apply(self.runtime, self.operation, True)
        self.assertFalse((self.install / "old.py").exists())
        self.assertTrue((self.install / "custom.py").exists())
        pending = sb.recover(self.runtime, self.operation, True)
        self.assertEqual(pending["status"], "awaiting_rollback_registration")
        self.assertEqual((self.install / "old.py").read_bytes(), b"owned old")
        self.assertTrue(sb.recover(self.runtime, self.operation, True, self.snapshot)["success"])

    def test_apply_requires_approval_and_detects_changed_preview(self):
        self.setup_install()
        with self.assertRaises(sb.AdapterError):
            sb.install_apply(self.runtime, self.operation)
        (self.install / "SKILL.md").write_bytes(skill("0.5.1", "external"))
        with self.assertRaises(sb.AdapterError):
            sb.install_apply(self.runtime, self.operation, True)
        self.assertIn(b"external", (self.install / "SKILL.md").read_bytes())
        self.assertEqual(sb.read_json(self.journal)["stage"], "incomplete")

    def test_missing_host_verification_never_success_and_mismatch_retained(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        for proof in (self.proof(instructions="different"), self.proof(enabled=True),
                      self.proof(id="different"), self.proof(success=False)):
            with self.assertRaises(sb.AdapterError):
                sb.install_verify(self.runtime, self.operation, proof)
            self.assertEqual(sb.read_json(self.journal)["stage"], "registration_mismatch")
        self.assertTrue(sb.install_verify(self.runtime, self.operation, self.proof())["success"])

    def test_host_wrapper_regeneration_restore_only_matching_body(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        path = self.install / "SKILL.md"
        path.write_bytes(skill().replace(b'version: "0.6.0"', b'version: "0.6.9"', 1))
        with self.assertRaises(sb.AdapterError):
            sb.install_verify(self.runtime, self.operation, self.proof())
        self.assertTrue(sb.install_verify(self.runtime, self.operation, self.proof(), True)["success"])
        self.assertEqual(path.read_bytes(), skill())

    def test_stale_restored_wrapper_refused(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        (self.install / "SKILL.md").write_bytes(skill("0.5.1"))
        with self.assertRaises(sb.AdapterError):
            sb.install_verify(self.runtime, self.operation, self.proof(), True)
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill("0.5.1"))

    def test_host_wrapper_without_package_version_is_safely_canonicalized(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        path = self.install / "SKILL.md"
        wrapper = skill().replace(b'version: "0.6.0"\n', b"", 1)
        path.write_bytes(wrapper)
        proof = self.proof(instructions=wrapper.decode())
        self.assertTrue(sb.install_verify(self.runtime, self.operation, proof, True)["success"])
        self.assertEqual(path.read_bytes(), skill())

    def test_interrupted_update_persistent_journal_and_recovery(self):
        self.setup_install(extras={"scripts/secondbrain.py": b"# fixture"})
        def interrupt(_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            sb.install_apply(self.runtime, self.operation, True, after_step=interrupt)
        self.assertEqual(sb.read_json(self.journal)["stage"], "applying")
        original_journal = self.journal.read_bytes()
        preview = sb.recover(self.runtime, self.operation)
        self.assertFalse(preview["success"])
        self.assertFalse(preview["mutated"])
        self.assertEqual(self.journal.read_bytes(), original_journal)
        self.assertFalse(sb.recover(self.runtime, self.operation, True)["success"])
        self.assertTrue(sb.recover(self.runtime, self.operation, True, self.snapshot)["success"])
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill("0.5.1"))

    def test_pending_after_commit_before_journal_save_can_recover(self):
        self.setup_install()
        journal = sb.read_json(self.journal)
        entry = journal["entries"]["SKILL.md"]
        entry["state"] = "pending"
        journal["stage"] = "applying"
        sb.save_json(self.journal, journal)
        (self.install / "SKILL.md").write_bytes(skill())
        self.assertFalse(sb.recover(self.runtime, self.operation, True)["success"])
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill("0.5.1"))

    def test_rollback_only_operation_created_files_and_conflict_protection(self):
        self.setup_install(extras={"scripts/secondbrain.py": b"# fixture"})
        sb.install_apply(self.runtime, self.operation, True)
        path = self.install / "scripts" / "secondbrain.py"
        path.write_bytes(b"# external edit")
        preview = sb.recover(self.runtime, self.operation)
        self.assertTrue(any(item["action"] == "blocked" for item in preview["files"]))
        with self.assertRaises(sb.AdapterError):
            sb.recover(self.runtime, self.operation, True)
        self.assertEqual(path.read_bytes(), b"# external edit")
        self.assertEqual(sb.read_json(self.journal)["stage"], "rollback_blocked_or_failed")

    def test_absent_created_files_removed_on_approved_recovery(self):
        self.setup_install(extras={"scripts/secondbrain.py": b"# fixture"}, legacy=True)
        sb.install_apply(self.runtime, self.operation, True)
        sb.recover(self.runtime, self.operation, True)
        self.assertFalse((self.install / "scripts" / "secondbrain.py").exists())
        self.assertFalse((self.install / "package-manifest.json").exists())
        self.assertTrue((self.install / "SKILL.md").exists())

    def test_rollback_failed_backup_preserved_incomplete_state(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        journal = sb.read_json(self.journal)
        backup = self.journal.parent / journal["entries"]["SKILL.md"]["backup"]
        backup.write_bytes(b"corrupt backup")
        with self.assertRaises(sb.AdapterError):
            sb.recover(self.runtime, self.operation, True)
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill())
        self.assertTrue(self.journal.exists())

    def test_rollback_host_verification_mismatch_not_success(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        with self.assertRaises(sb.AdapterError):
            sb.recover(self.runtime, self.operation, True, self.proof())
        self.assertTrue(sb.recover(self.runtime, self.operation, True, self.snapshot)["success"])

    def test_rollback_host_generated_wrapper_requires_real_old_readback(self):
        self.setup_install()
        sb.install_apply(self.runtime, self.operation, True)
        sb.recover(self.runtime, self.operation, True)
        path = self.install / "SKILL.md"
        wrapper = skill("0.5.1").replace(b'version: "0.5.1"\n', b"", 1)
        path.write_bytes(wrapper)
        proof = {**self.snapshot, "instructions": wrapper.decode()}
        preview = sb.recover(self.runtime, self.operation)
        self.assertEqual(next(item["action"] for item in preview["files"]
                              if item["path"] == "SKILL.md"), "restore_host_wrapper")
        self.assertTrue(sb.recover(self.runtime, self.operation, True, proof)["success"])
        self.assertEqual(path.read_bytes(), skill("0.5.1"))

    def test_prepare_refuses_existing_disk_registration_mismatch(self):
        package = self.package()
        install = self.package("installed", "0.5.1")
        with self.assertRaises(sb.AdapterError):
            sb.install_prepare(self.runtime, package, install,
                               {"success": True, "name": "second-brain", "instructions": "wrong"})

    def test_staged_bytes_tampering_blocks_install(self):
        self.setup_install()
        journal = sb.read_json(self.journal)
        candidate = self.journal.parent / journal["entries"]["SKILL.md"]["candidate"]
        candidate.write_bytes(skill(note="tampered"))
        with self.assertRaises(sb.AdapterError):
            sb.install_apply(self.runtime, self.operation, True)
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill("0.5.1"))

    def test_new_manifest_cannot_overwrite_local_only_adapter(self):
        package = self.package(extras={"scripts/secondbrain.py": b"# package"})
        install = self.package("installed", "0.5.1")
        local = install / "scripts" / "secondbrain.py"
        local.parent.mkdir()
        local.write_bytes(b"# private adapter")
        snapshot = {"success": True, "name": "second-brain", "id": "fixture",
                    "instructions": sb.skill_candidate(skill("0.5.1"))[1]}
        with self.assertRaises(sb.AdapterError):
            sb.install_prepare(self.runtime, package, install, snapshot)
        self.assertEqual(local.read_bytes(), b"# private adapter")

    def test_malformed_journal_is_explicitly_refused(self):
        self.setup_install()
        journal = sb.read_json(self.journal)
        journal["entries"] = []
        self.journal.write_text(json.dumps(journal), encoding="utf-8")
        with self.assertRaises(sb.AdapterError):
            sb.install_apply(self.runtime, self.operation, True)
        self.assertEqual((self.install / "SKILL.md").read_bytes(), skill("0.5.1"))

    def test_backup_failure_retains_operation_info_before_any_install(self):
        package = self.package()
        install = self.package("installed", "0.5.1")
        snapshot = {"success": True, "name": "second-brain", "id": "fixture",
                    "instructions": sb.skill_candidate(skill("0.5.1"))[1]}
        real = sb.write_bytes
        def fail_backup(path, *args, **kwargs):
            if path.name.startswith("before-"):
                raise sb.AdapterError("fixture backup failure")
            return real(path, *args, **kwargs)
        with patch.object(sb, "write_bytes", fail_backup):
            with self.assertRaises(sb.AdapterError) as error:
                sb.install_prepare(self.runtime, package, install, snapshot)
        self.assertIn("operation", error.exception.details)
        self.assertTrue(Path(error.exception.details["journalPath"]).exists())
        self.assertEqual((install / "SKILL.md").read_bytes(), skill("0.5.1"))


class CliTests(Fixture):
    def invoke(self, args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = sb.main(args)
        return code, json.loads(stream.getvalue())

    def test_capabilities_json(self):
        code, obj = self.invoke(["capabilities"])
        self.assertEqual(code, 0)
        self.assertFalse(obj["helperBacked"]["multiFileAtomic"])
        self.assertFalse(obj["instructionOnly"]["executableGuarantees"])

    def test_guarded_write_and_rejection_json(self):
        content = self.base / "content"
        content.write_bytes(b"fixture")
        args = ["guarded-write", "--runtime-dir", str(self.runtime), "--root", str(self.root),
                "--target", "context.md", "--expected-sha256", "absent", "--content-file", str(content)]
        code, obj = self.invoke(args)
        self.assertEqual(code, 1)
        self.assertFalse(obj["success"])
        code, obj = self.invoke(args + ["--approved"])
        self.assertEqual(code, 0)
        self.assertEqual(obj["status"], "written")

    def test_validate_package_cli(self):
        package = self.package()
        code, obj = self.invoke(["validate-package", "--package-dir", str(package)])
        self.assertEqual(code, 0)
        self.assertEqual(obj["authenticity"], "not_verified")

    def test_real_checkout_cli_validates_and_writes_disposable_unlinked_fixtures(self):
        self.assertEqual(sb.PACKAGE_ROOT, MODULE_PATH.parent.parent)
        self.assertEqual(sb.inspect_path(sb.PACKAGE_ROOT), sb.PACKAGE_ROOT)
        package = self.package()
        content = self.base / "approved-content"
        content.write_bytes(b"approved disposable fixture")
        commands = [
            ["validate-package", "--package-dir", str(package)],
            ["guarded-write", "--runtime-dir", str(self.runtime), "--root", str(self.root),
             "--target", "context.md", "--expected-sha256", "absent",
             "--content-file", str(content), "--approved"],
        ]
        for args in commands:
            result = subprocess.run([sys.executable, str(MODULE_PATH), *args],
                                    cwd=sb.PACKAGE_ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertTrue(json.loads(result.stdout)["success"])
        self.assertEqual((self.root / "context.md").read_bytes(), content.read_bytes())

    def test_executable_cli_install_smoke_and_missing_now_json(self):
        package = self.package(extras={
            "scripts/fixture.py": b"raise RuntimeError('Package scripts must never execute during installation')\n"})
        install = self.package("installed", "0.5.1")
        snapshot = {"success": True, "name": "second-brain", "id": "fixture",
                    "enabled": False, "resourceDir": str(install),
                    "instructions": sb.skill_candidate(skill("0.5.1"))[1]}
        snapshot_path = self.base / "snapshot.json"
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        def run(*args):
            result = subprocess.run([sys.executable, str(MODULE_PATH), *args],
                                    capture_output=True, text=True)
            self.assertEqual(result.stderr, "")
            return result.returncode, json.loads(result.stdout)
        code, prepared = run("install-prepare", "--runtime-dir", str(self.runtime),
                             "--package-dir", str(package), "--install-dir", str(install),
                             "--registration-snapshot", str(snapshot_path))
        self.assertEqual(code, 0)
        shared = ["--runtime-dir", str(self.runtime), "--operation", prepared["operation"]]
        code, applied = run("install-apply", *shared, "--approved")
        self.assertEqual(code, 1)
        self.assertEqual(applied["registrationRequest"]["tool"], "m_update_skill")
        code, unverified = run("install-verify", *shared)
        self.assertEqual(code, 1)
        self.assertFalse(unverified["success"])
        # A trusted host-exported fixture readback; no actual registry is touched.
        snapshot["instructions"] = sb.skill_candidate(skill())[1]
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        code, verified = run("install-verify", *shared, "--registration-readback", str(snapshot_path))
        self.assertEqual(code, 0)
        self.assertEqual(verified["status"], "completed")
        code, rejected = run("check-updates", "--runtime-dir", str(self.runtime),
                             "--installed-version", "0.5.1")
        self.assertEqual(code, 2)
        self.assertEqual(rejected["status"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
