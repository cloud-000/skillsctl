import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skillsctl.bundle import Error, canonical, fetch, inventory, normalize, sha, validate
from skillsctl.manager import Manager, atomic_json


def fixture(path, version="1", dependencies=False, body=None):
    path.mkdir(parents=True, exist_ok=True)
    manifest = {"schemaVersion": 1, "id": "demo", "version": version, "variants": {}}
    for target in ("codex", "antigravity"):
        base = path / "bundles" / target
        components = []
        for name in ("alpha", "beta"):
            skill = base / "skills" / name
            skill.mkdir(parents=True, exist_ok=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: A {name} skill\n---\n{body or version}: {target}\n")
            helper = skill / "run.sh"
            helper.write_text("#!/bin/sh\nexit 0\n")
            helper.chmod(0o755)
            components.append({"id": "skill:" + name, "kind": "skill", "path": "skills/" + name,
                               "requires": ["skill:beta"] if name == "alpha" and dependencies else []})
        manifest["variants"][target] = {"root": "bundles/" + target, "components": components, "files": inventory(base)}
    (path / "bundle.json").write_bytes(canonical(manifest))
    return path


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.manager = Manager(self.home)
        self.source = fixture(self.root / "source")

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, fn, dry_run=False):
        with self.manager.locked():
            before = self.manager.state()
            after = copy.deepcopy(before)
            fn(after)
            return self.manager.commit(before, after, dry_run)

    def install(self, **kwargs):
        return self.apply(lambda s: self.manager.install(s, str(self.source), ["codex", "antigravity"], **kwargs))

    def active(self, target="codex", name="alpha"):
        return self.manager.discovery(target) / name

    def test_full_lifecycle_independent_harnesses_and_rollback(self):
        first = self.install()
        self.assertIn("1: codex", (self.active() / "SKILL.md").read_text())
        self.assertTrue(os.access(self.active() / "run.sh", os.X_OK))
        self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", False, "alpha"))
        self.assertFalse(self.active().exists())
        self.assertTrue(self.active("antigravity").exists())
        self.assertTrue(self.active(name="beta").exists())
        self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", False))
        self.assertFalse(self.active(name="beta").exists())
        self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", True))
        self.assertFalse(self.active().exists())
        self.assertTrue(self.active(name="beta").exists())
        self.apply(lambda s: self.manager.rollback(s, ["codex"], "demo", first["generation"]))
        self.assertTrue(self.active().exists())
        fixture(self.source, version="2")
        self.apply(lambda s: self.manager.install(s, str(self.source), ["codex"], update="demo"))
        self.assertIn("2: codex", (self.active() / "SKILL.md").read_text())
        self.apply(lambda s: self.manager.rollback(s, ["codex"], "demo", skill="alpha"))
        self.assertIn("1: codex", (self.active() / "SKILL.md").read_text())
        self.assertIn("2: codex", (self.active(name="beta") / "SKILL.md").read_text())
        self.apply(lambda s: s["packages"]["codex"].pop("demo"))
        self.assertFalse(self.active().exists())
        self.assertTrue(self.active("antigravity").exists())

    def test_dependency_disable_and_rollback_require_cascade(self):
        fixture(self.source, dependencies=True)
        self.install(skills=["alpha"])
        with self.assertRaisesRegex(Error, "Required by"):
            self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", False, "beta"))
        self.assertTrue(self.active().exists())
        self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", False, "beta", True))
        self.assertFalse(self.active().exists())
        self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", True, "alpha"))
        fixture(self.source, "2", dependencies=True)
        self.apply(lambda s: self.manager.install(s, str(self.source), ["codex"], update="demo"))
        with self.assertRaisesRegex(Error, "dependency cohort"):
            self.apply(lambda s: self.manager.rollback(s, ["codex"], "demo", skill="beta"))
        self.apply(lambda s: self.manager.rollback(s, ["codex"], "demo", skill="beta", cascade=True))
        self.assertIn("1: codex", (self.active() / "SKILL.md").read_text())

    def test_updates_preserve_disabled_and_do_not_add_unselected_skills(self):
        self.install(skills=["alpha"])
        fixture(self.source, "2")
        self.apply(lambda s: self.manager.install(s, str(self.source), ["codex"], update="demo"))
        self.assertFalse(self.active(name="beta").exists())
        self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", False, "alpha"))
        fixture(self.source, "3")
        self.apply(lambda s: self.manager.install(s, str(self.source), ["codex"], update="demo"))
        self.assertFalse(self.active().exists())

    def test_unmanaged_collision_does_not_partially_deploy(self):
        self.active("antigravity").mkdir(parents=True)
        (self.active("antigravity") / "personal.txt").write_text("mine")
        with self.assertRaisesRegex(Error, "Unmanaged"):
            self.install()
        self.assertFalse(self.active().exists())
        self.assertEqual((self.active("antigravity") / "personal.txt").read_text(), "mine")
        self.assertIsNone(self.manager.state()["generation"])

    def test_dry_run_does_not_change_harness_or_state(self):
        result = self.apply(lambda s: self.manager.install(s, str(self.source), ["codex"]), dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertFalse(self.manager.discovery("codex").exists())
        self.assertIsNone(self.manager.state()["generation"])

    def test_local_changes_are_never_overwritten(self):
        self.install()
        p = self.active() / "SKILL.md"
        p.chmod(0o644)
        p.write_text("my edited instructions")
        with self.assertRaisesRegex(Error, "modified"):
            self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", False))
        self.assertEqual(p.read_text(), "my edited instructions")

    def test_crash_before_commit_recovers_previous_links(self):
        self.install()
        before = self.manager.state()
        fake = copy.deepcopy(before)
        fake["generation"] = "interrupted"
        fake["links"] = {}
        atomic_json(self.manager.root / "journal.json", {"before": before, "after": fake})
        self.manager.change_links(before["links"], {})
        self.assertFalse(self.active().exists())
        with self.manager.locked():
            self.manager.check(self.manager.state())
        self.assertTrue(self.active().exists())
        self.assertFalse((self.manager.root / "journal.json").exists())

    def test_crash_after_commit_finishes_and_keeps_new_state(self):
        self.install()
        before = self.manager.state()
        self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", False))
        after = self.manager.state()
        atomic_json(self.manager.root / "journal.json", {"before": before, "after": after})
        with self.manager.locked():
            self.manager.check(self.manager.state())
        self.assertFalse(self.active().exists())
        self.assertEqual(self.manager.state()["generation"], after["generation"])

    def test_failed_link_write_restores_all_harnesses(self):
        self.install()
        before = self.manager.state()
        original = os.replace
        calls = 0

        def fail_once(src, dst):
            nonlocal calls
            if str(src).split("/")[-1].startswith(".skillsctl-"):
                calls += 1
                if calls == 2:
                    raise OSError("simulated disk failure")
            return original(src, dst)

        with patch("skillsctl.manager.os.replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", False, "alpha"))
        self.manager.check(self.manager.state())
        self.assertEqual(self.manager.state(), before)

    def test_repeated_enable_is_idempotent(self):
        self.install()
        result = self.apply(lambda s: self.manager.toggle(s, ["codex"], "demo", True))
        self.assertTrue(result["unchanged"])

    def test_store_tampering_detected_on_next_process(self):
        self.install()
        state = self.manager.state()
        digest = state["packages"]["codex"]["demo"]["digest"]
        p = self.manager.store / digest / "bundles/codex/skills/alpha/SKILL.md"
        p.chmod(0o644)
        p.write_text("tampered")
        with self.assertRaisesRegex(Error, "Checksum"):
            Manager(self.home).bundle(digest)


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_standalone_skill_without_pstack(self):
        src = self.root / "hello"
        src.mkdir()
        (src / "SKILL.md").write_text("---\nname: hello\ndescription: Say hello\n---\nHello\n")
        m = normalize(src, self.root / "bundle")
        self.assertEqual(m["id"], "hello")
        validate(self.root / "bundle")

    def test_unrelated_native_plugin_with_resources(self):
        src = self.root / "plugin"
        (src / "skills/hello").mkdir(parents=True)
        (src / "skills/hello/SKILL.md").write_text("---\nname: hello\ndescription: Say hello\n---\nSee ../../guide.txt\n")
        (src / "plugin.json").write_text('{"name":"greetings","version":"1.0.0"}')
        (src / "guide.txt").write_text("greetings")
        m = normalize(src, self.root / "bundle")
        self.assertEqual(m["id"], "greetings")
        self.assertIn("guide.txt", m["variants"]["codex"]["files"])

    def test_traversal_unknown_dependency_and_candidate_rejected(self):
        path = fixture(self.root / "fixture")
        good = json.loads((path / "bundle.json").read_text())
        for mutate in (lambda m: m["variants"]["codex"].update(root="../escape"),
                       lambda m: m["variants"]["codex"]["components"][0].update(requires=["skill:missing"]),
                       lambda m: m.update(status="candidate")):
            m = copy.deepcopy(good)
            mutate(m)
            (path / "bundle.json").write_bytes(canonical(m))
            with self.assertRaises(Error):
                validate(path)

    def test_checksum_and_symlink_rejected(self):
        path = fixture(self.root / "fixture")
        p = path / "bundles/codex/skills/alpha/SKILL.md"
        p.write_text("bad")
        with self.assertRaisesRegex(Error, "Checksum"):
            validate(path)
        p.unlink()
        p.symlink_to("/etc/passwd")
        with self.assertRaisesRegex(Error, "symlink"):
            validate(path)

    def test_native_hook_plugin_not_silently_downgraded(self):
        src = self.root / "plugin"
        (src / "hooks").mkdir(parents=True)
        (src / "plugin.json").write_text('{"name":"hooked"}')
        with self.assertRaisesRegex(Error, "non-skill"):
            normalize(src, self.root / "bundle")

    def test_https_archive_integrity_and_extraction(self):
        source = fixture(self.root / "source")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for p in source.rglob("*"):
                tar.add(p, arcname="release/" + p.relative_to(source).as_posix(), recursive=False)
        data = archive.getvalue()
        dest = self.root / "download"
        dest.mkdir()
        with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
            unpacked = fetch("https://example.test/release.tar.gz", dest, sha(data))
        self.assertEqual(validate(unpacked)["id"], "demo")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
            with self.assertRaisesRegex(Error, "checksum"):
                fetch("https://example.test/release.tar.gz", dest, "0" * 64)

    def test_archive_traversal_rejected_before_extraction(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            info = tarfile.TarInfo("../escape")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"oops"))
        data = archive.getvalue()
        dest = self.root / "download"
        dest.mkdir()
        with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
            with self.assertRaisesRegex(Error, "Unsafe"):
                fetch("https://example.test/release.tar.gz", dest, sha(data))
        self.assertFalse((self.root / "escape").exists())


if __name__ == "__main__":
    unittest.main()
