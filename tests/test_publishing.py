"""Synthetic IPA fixtures and local bare Git remotes; never publish real releases."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import copy
import datetime as dt
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_ipa as pub
import validate_source as validator


def empty_source():
    return {"name": "My Private Apps", "identifier": pub.SOURCE_ID,
            "sourceURL": pub.SOURCE_URL, "apps": [], "news": []}


def arguments(ipa=None, **changes):
    result = argparse.Namespace(ipa=ipa, init=True, name=None, developer="Fixture Developer",
                                description="Synthetic fixture only", icon_url="https://example.org/icon.png",
                                notes="Fixture release notes", inspect=False, dry_run=False, resume=False)
    for key, value in changes.items():
        setattr(result, key, value)
    return result


def metadata(version="1.0.0", build="1"):
    return {"name": "Fixture App", "bundleIdentifier": "com.example.fixture",
            "version": version, "build": build, "size": 123, "minOSVersion": "16.0"}


def one_app_source():
    return pub.candidate_source(empty_source(), metadata(), arguments(), "2024-01-01T00:00:00Z",
                                pub.download_url(*pub.release_identity(metadata())))


def write_ipa(path, *, binary=True, version="1.0.0", build="7", bundle="com.example.fixture"):
    info = {"CFBundleIdentifier": bundle, "CFBundleShortVersionString": version,
            "CFBundleVersion": build, "CFBundleDisplayName": "Fixture App",
            "CFBundleName": "Fallback", "MinimumOSVersion": "16.0"}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Payload/Fixture.app/Info.plist", plistlib.dumps(
            info, fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML))
        archive.writestr("Payload/Fixture.app/PlugIns/Share.appex/Info.plist", plistlib.dumps(info))
        archive.writestr("Payload/Fixture.app/Fixture", b"synthetic-not-executable")


class MetadataAndSourceTests(unittest.TestCase):
    def test_binary_and_xml_plist_with_extension_and_spaces(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "My Fixture App.ipa"
            for binary in (True, False):
                write_ipa(path, binary=binary)
                result = pub.ipa_metadata(path)
                self.assertEqual(result["build"], "7")
                self.assertEqual(result["bundleIdentifier"], "com.example.fixture")
                self.assertEqual(result["size"], path.stat().st_size)
                self.assertEqual(result["minOSVersion"], "16.0")

    def test_shell_inspect_reads_fixture_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "My Fixture App.ipa"
            write_ipa(path)
            wrapper = Path(__file__).resolve().parents[1] / "scripts" / "upload-ipa.sh"
            result = subprocess.run(["bash", wrapper, path, "--inspect"], check=True,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            actual = json.loads(result.stdout)
            self.assertEqual(actual["bundleIdentifier"], "com.example.fixture")
            self.assertEqual(actual["size"], path.stat().st_size)

    def test_missing_or_multiple_main_plist_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "broken.ipa"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("NotPayload/Info.plist", b"invalid")
            with self.assertRaises(pub.PublishError):
                pub.ipa_metadata(path)
            write_ipa(path)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("Payload/Second.app/Info.plist", plistlib.dumps({}))
            with self.assertRaises(pub.PublishError):
                pub.ipa_metadata(path)

    def test_init_requires_explicit_metadata(self):
        with self.assertRaisesRegex(pub.PublishError, "--developer"):
            pub.candidate_source(empty_source(), metadata(), arguments(developer=None),
                                 "2024-01-01", "https://example.org/file.ipa")
        with self.assertRaisesRegex(pub.PublishError, "Bundle ID"):
            pub.candidate_source(empty_source(), metadata(), arguments(init=False),
                                 "2024-01-01", "https://example.org/file.ipa")

    def test_bundle_mismatch_without_init_stops(self):
        changed = metadata("2.0.0")
        changed["bundleIdentifier"] = "com.example.other"
        with self.assertRaisesRegex(pub.PublishError, "不在源中"):
            pub.candidate_source(one_app_source(), changed, arguments(init=False),
                                 "2024-02-01", "https://example.org/file.ipa")

    def test_updates_keep_all_history_and_compare_numerically(self):
        source = one_app_source()
        update = arguments(init=False, developer=None, description=None, icon_url=None)
        for day, version in enumerate(("1.1.0", "1.2.0", "1.9.0", "1.10.0"), start=2):
            before = copy.deepcopy(source)
            source = pub.candidate_source(source, metadata(version), update,
                                          f"2024-01-{day:02}", "https://example.org/file.ipa")
            self.assertEqual(source["apps"][0]["versions"][1:], before["apps"][0]["versions"])
        self.assertEqual(len(source["apps"][0]["versions"]), 5)
        self.assertEqual(source["apps"][0]["versions"][0]["version"], "1.10.0")
        for version in ("1.9.9", "1.10", "1.10.0"):
            with self.assertRaisesRegex(pub.PublishError, "必须大于"):
                pub.candidate_source(source, metadata(version, "99"), update,
                                     "2024-02-01", "https://example.org/file.ipa")

    def test_release_url_uses_bundle_version_build_not_latest(self):
        tag, name = pub.release_identity(metadata())
        self.assertEqual(tag, "com.example.fixture-v1.0.0-b1")
        self.assertEqual(pub.download_url(tag, name),
                         "https://github.com/athlon128/AppStore/releases/download/"
                         "com.example.fixture-v1.0.0-b1/com.example.fixture-1.0.0-1.ipa")

    def test_official_schema_and_semantic_failures_have_field_paths(self):
        changes = [
            (lambda s: s["apps"].append(copy.deepcopy(s["apps"][0])), "bundleIdentifier"),
            (lambda s: s["apps"][0].update(Build="7"), "Build"),
            (lambda s: s["apps"][0].update(marketplaceID="123"), "marketplaceID"),
            (lambda s: s["apps"][0].update(iconURL="http://example.org/icon.png"), "iconURL"),
            (lambda s: s["apps"][0]["versions"][0].update(size=True), "size"),
            (lambda s: s["apps"][0]["versions"][0].update(size=1.5), "size"),
            (lambda s: s["apps"][0]["versions"][0].update(size=0), "size"),
            (lambda s: s["apps"][0]["versions"][0].update(date="2024-02-30"), "date"),
            (lambda s: s["apps"][0]["versions"][0].update(date="2024-01-01T00:00:00"), "date"),
            (lambda s: s["apps"][0]["versions"][0].update(version=""), "version"),
            (lambda s: s["apps"][0]["versions"][0].pop("downloadURL"), "downloadURL"),
        ]
        validator.validate_source(empty_source())
        for change, field in changes:
            with self.subTest(field=field):
                source = one_app_source()
                change(source)
                with self.assertRaisesRegex(validator.ValidationError, field):
                    validator.validate_source(source)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(validator.ValidationError):
            json.loads('{"apps": [], "apps": []}', object_pairs_hook=validator.no_duplicate_keys)


class PublicationFlowTests(unittest.TestCase):
    """Actual local git commit/push; mock only GitHub HTTP and release commands."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="appstore-test-")
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name).resolve()
        self.root = folder / "repo"
        self.root.mkdir()
        self.remote = folder / "remote.git"
        self.ipa = folder / "My App with spaces.ipa"
        write_ipa(self.ipa)
        self.raw_git("init", "--initial-branch=main")
        self.raw_git("config", "user.name", "Fixture")
        self.raw_git("config", "user.email", "fixture@example.org")
        self.source_path = self.root / "source.json"
        self.source_path.write_text(json.dumps(empty_source()), encoding="utf-8")
        self.raw_git("add", "source.json")
        self.raw_git("commit", "-m", "Fixture source")
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", self.remote],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.raw_git("remote", "add", "origin", str(self.remote))
        self.raw_git("push", "-u", "origin", "main")
        self.initial_head = self.raw_git("rev-parse", "HEAD")
        self.release = None
        self.calls = []
        self.fail_upload = False
        self.fail_push = False
        self.bad_remote = False
        self.permission = True
        self.original_run = pub.run
        self.original_git = pub.git
        for context in (patch.object(pub, "ROOT", self.root),
                        patch.object(pub, "run", self.fake_run),
                        patch.object(pub, "git", self.fake_git),
                        patch.object(pub, "api", self.fake_api)):
            context.start()
            self.addCleanup(context.stop)

    def raw_git(self, *args):
        result = subprocess.run(["git", *map(str, args)], cwd=self.root, check=True,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.strip()

    def fake_git(self, *args):
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/wrong/repo.git" if self.bad_remote else f"https://github.com/{pub.REPO}.git"
        if args[0] == "push" and self.fail_push:
            raise pub.PublishError("simulated push failure")
        return self.original_git(*args)

    def fake_api(self, endpoint, *, paginate=False):
        if endpoint == f"repos/{pub.REPO}":
            return {"private": False, "permissions": {"push": self.permission}}
        if "/releases?" in endpoint:
            return [copy.deepcopy(self.release)] if self.release else []
        if "/git/matching-refs/" in endpoint:
            return []
        if endpoint == f"repos/{pub.REPO}/releases/101":
            return copy.deepcopy(self.release)
        raise AssertionError(endpoint)

    def fake_run(self, args, *, cwd=None):
        if args[0] != "gh":
            return self.original_run(args, cwd=self.root)
        self.calls.append(tuple(str(item) for item in args))
        if args[1:3] == ["auth", "status"]:
            return "logged in (mock)"
        if args[1:3] == ["release", "create"]:
            self.release = {"id": 101, "tag_name": args[3], "draft": True, "prerelease": False,
                            "published_at": None, "assets": []}
        elif args[1:3] == ["release", "upload"]:
            if self.fail_upload:
                raise pub.PublishError("simulated upload failure")
            asset = Path(args[4])
            self.release["assets"] = [{"name": asset.name, "size": asset.stat().st_size,
                "state": "uploaded", "digest": f"sha256:{pub.sha256(asset)}",
                "browser_download_url": f"https://github.com/{pub.REPO}/releases/download/untagged-fixture123/{asset.name}"}]
        elif args[1:3] == ["release", "edit"]:
            self.release.update(draft=False, published_at="2026-10-28T08:00:00Z")
            for asset in self.release["assets"]:
                asset["browser_download_url"] = pub.download_url(self.release["tag_name"], asset["name"])
        else:
            raise AssertionError(args)
        return ""

    def publish(self, **changes):
        output, errors = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(errors):
                pub.publish(arguments(self.ipa, **changes))
        finally:
            self.last_output = output.getvalue()
            self.last_errors = errors.getvalue()
        return output.getvalue(), errors.getvalue()

    def seed_release(self, *, wrong_digest=False):
        meta = pub.ipa_metadata(self.ipa)
        tag, name = pub.release_identity(meta)
        self.release = {"id": 101, "tag_name": tag, "draft": False, "prerelease": False,
                        "published_at": "2026-10-28T08:00:00Z", "assets": [
            {"name": name, "size": meta["size"], "state": "uploaded",
             "digest": "sha256:" + ("0" * 64 if wrong_digest else pub.sha256(self.ipa)),
             "browser_download_url": pub.download_url(tag, name)}]}

    def test_publish_reads_actual_bytes_and_pushes_only_source(self):
        output, _ = self.publish()
        source = validator.validate_source(validator.load_json(self.source_path))
        version = source["apps"][0]["versions"][0]
        self.assertEqual(version["size"], self.ipa.stat().st_size)
        self.assertEqual(version["date"], self.release["published_at"])
        self.assertFalse(self.release["draft"])
        self.assertIn("发布完成", output)
        self.assertEqual(self.raw_git("status", "--porcelain"), "")
        self.assertEqual(self.raw_git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "source.json")
        remote_source = subprocess.run(["git", "--git-dir", self.remote, "show", "main:source.json"],
                                      check=True, text=True, stdout=subprocess.PIPE).stdout
        self.assertEqual(json.loads(remote_source), source)

    def test_duplicate_release_is_never_overwritten(self):
        self.seed_release()
        with self.assertRaisesRegex(pub.PublishError, "已存在"):
            self.publish()
        self.assertFalse(any(call[1:3] in (("release", "create"), ("release", "upload")) for call in self.calls))
        self.assertEqual(self.raw_git("rev-parse", "HEAD"), self.initial_head)

    def test_resume_checks_digest_and_never_uploads(self):
        self.seed_release()
        self.publish(resume=True)
        self.assertEqual(len(validator.load_json(self.source_path)["apps"]), 1)
        self.assertFalse(any(call[1:3] in (("release", "create"), ("release", "upload")) for call in self.calls))

    def test_resume_rejects_different_ipa(self):
        self.seed_release(wrong_digest=True)
        with self.assertRaisesRegex(pub.PublishError, "SHA-256"):
            self.publish(resume=True)
        self.assertEqual(self.raw_git("rev-parse", "HEAD"), self.initial_head)

    def test_resume_draft_rechecks_final_tag_url_before_source_update(self):
        self.seed_release()
        self.release.update(draft=True, published_at=None)
        asset = self.release["assets"][0]
        asset["browser_download_url"] = f"https://github.com/{pub.REPO}/releases/download/untagged-fixture123/{asset['name']}"
        self.publish(resume=True)
        self.assertFalse(self.release["draft"])
        version = validator.load_json(self.source_path)["apps"][0]["versions"][0]
        self.assertEqual(version["downloadURL"], pub.download_url(self.release["tag_name"], asset["name"]))
        self.assertNotIn("untagged-", version["downloadURL"])
        self.assertFalse(any(call[1:3] == ("release", "upload") for call in self.calls))

    def test_published_release_rejects_temporary_draft_url(self):
        self.seed_release()
        asset = self.release["assets"][0]
        asset["browser_download_url"] = f"https://github.com/{pub.REPO}/releases/download/untagged-fixture123/{asset['name']}"
        with self.assertRaisesRegex(pub.PublishError, "实际下载地址"):
            self.publish(resume=True)
        self.assertEqual(self.raw_git("rev-parse", "HEAD"), self.initial_head)

    def test_dirty_worktree_stops_before_release(self):
        (self.root / "uncommitted.txt").write_text("user data", encoding="utf-8")
        with self.assertRaisesRegex(pub.PublishError, "工作区不干净"):
            self.publish()
        self.assertIsNone(self.release)
        self.assertEqual((self.root / "uncommitted.txt").read_text(), "user data")

    def test_wrong_remote_and_missing_permission_stop(self):
        self.bad_remote = True
        with self.assertRaisesRegex(pub.PublishError, "origin"):
            self.publish()
        self.bad_remote = False
        self.permission = False
        with self.assertRaisesRegex(pub.PublishError, "push 权限"):
            self.publish()
        self.assertIsNone(self.release)

    def test_dry_run_does_not_create_release_or_commit(self):
        output, _ = self.publish(dry_run=True)
        self.assertIn("dry-run", output)
        self.assertIsNone(self.release)
        self.assertEqual(self.raw_git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(validator.load_json(self.source_path)["apps"], [])

    def test_upload_failure_preserves_draft_and_source(self):
        self.fail_upload = True
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaisesRegex(pub.PublishError, "upload failure"):
            self.publish()
        self.assertTrue(self.release["draft"])
        self.assertEqual(self.raw_git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(validator.load_json(self.source_path)["apps"], [])

    def test_push_failure_retains_published_release_and_local_commit(self):
        self.fail_push = True
        with self.assertRaisesRegex(pub.PublishError, "push failure"):
            self.publish()
        self.assertFalse(self.release["draft"])
        self.assertNotEqual(self.raw_git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.raw_git("rev-parse", "origin/main"), self.initial_head)
        validator.validate_source(validator.load_json(self.source_path))
        self.assertIn("发布未全部完成", self.last_errors)
        self.assertIn("git push origin main", self.last_errors)
        self.assertNotIn("发布完成", self.last_output)


if __name__ == "__main__":
    unittest.main()
