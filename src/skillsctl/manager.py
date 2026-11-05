"""Immutable package store, selected components, and recoverable deployments."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .bundle import Error, TARGETS, canonical, fetch, inventory, normalize, read_json, sha, validate


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(canonical(data))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def empty_state():
    return {"schemaVersion": 1, "generation": None, "parent": None, "packages": {}, "links": {}, "files": {}}


class Manager:
    def __init__(self, home=None, root=None):
        self.home = Path(home or Path.home()).expanduser().resolve()
        self.root = Path(root or self.home / ".local/share/skillsctl").expanduser().resolve()
        self.store = self.root / "store"
        self.generations = self.root / "generations"
        self._cache = {}

    def discovery(self, target):
        if target == "codex":
            return self.home / ".agents/skills"
        if target == "antigravity":
            return self.home / ".gemini/config/skills"
        raise Error(f"Unsupported harness: {target}")

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Error("Another skillsctl operation is running") from exc
            self.recover()
            yield

    def state(self):
        p = self.root / "state.json"
        state = read_json(p) if p.exists() else empty_state()
        if state.get("schemaVersion") != 1:
            raise Error("Unsupported state schema")
        return state

    def bundle(self, digest):
        if digest not in self._cache:
            path = self.store / digest
            manifest = validate(path)
            if sha(canonical(manifest)) != digest:
                raise Error(f"Store manifest was modified: {digest}")
            self._cache[digest] = manifest
        return self._cache[digest]

    def import_source(self, source, targets, checksum=None):
        self.store.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.root, prefix="fetch-") as tmp:
            src = fetch(source, Path(tmp), checksum)
            bundle = Path(tmp) / "bundle"
            m = normalize(src, bundle, targets)
            for target in targets:
                if target not in m["variants"]:
                    raise Error(f"Package has no {target} variant")
            digest = sha(canonical(m))
            dest = self.store / digest
            if dest.exists():
                self.bundle(digest)
            else:
                shutil.move(bundle, dest)
                for p in dest.rglob("*"):
                    if p.is_file():
                        p.chmod(0o555 if p.stat().st_mode & 0o111 else 0o444)
            self._cache[digest] = m
            return digest, m

    def components(self, digest, target):
        return {c["id"]: c for c in self.bundle(digest)["variants"][target]["components"]}

    def closure(self, digest, target, ids):
        components = self.components(digest, target)
        result, todo = set(), list(ids)
        while todo:
            cid = todo.pop()
            if cid not in components:
                raise Error(f"Unknown component: {cid}")
            if cid not in result:
                result.add(cid)
                todo.extend(components[cid].get("requires", []))
        return result

    def install(self, state, source, targets, skills=None, checksum=None, update=None):
        digest, m = self.import_source(source, targets, checksum)
        if update and m["id"] != update:
            raise Error(f"Update source contains {m['id']}, expected {update}")
        for target in targets:
            packages = state["packages"].setdefault(target, {})
            old = packages.get(m["id"])
            if old and not update:
                raise Error(f"{m['id']} is already installed for {target}; use update")
            if update and not old:
                raise Error(f"{update} is not installed for {target}")
            cs = self.components(digest, target)
            requested = ["skill:" + s for s in skills] if skills else [cid for cid, c in cs.items() if c["kind"] == "skill"]
            if old:
                # Preserve the selected set for partial installs; whole installs pick up new skills.
                requested = [cid for cid, item in old["components"].items() if item["enabled"] and cid in cs] if not old["all"] else requested
            selected = self.closure(digest, target, requested)
            entries = {cid: {"digest": digest, "enabled": True} for cid in selected}
            if old:
                for cid, item in old["components"].items():
                    if cid in cs and not item["enabled"]:
                        entries[cid] = {"digest": digest, "enabled": False}
                # Preserve disabled preferences and detect incompatible requirements before deployment.
            packages[m["id"]] = {
                "digest": digest, "source": source, "checksum": checksum,
                "enabled": old["enabled"] if old else True,
                "all": old["all"] if old else not bool(skills), "components": entries,
            }

    def package(self, state, target, name):
        try:
            return state["packages"][target][name]
        except KeyError as exc:
            raise Error(f"{name} is not installed for {target}") from exc

    def toggle(self, state, targets, name, enabled, skill=None, cascade=False):
        for target in targets:
            pkg = self.package(state, target, name)
            if not skill:
                pkg["enabled"] = enabled
                continue
            cid = "skill:" + skill
            entries = pkg["components"]
            if enabled:
                digest = entries.get(cid, {}).get("digest", pkg["digest"])
                for dep in self.closure(digest, target, [cid]):
                    existing = entries.get(dep)
                    if existing and existing["digest"] != digest:
                        raise Error("Enable would mix dependency versions; update the package first")
                    entries[dep] = {"digest": digest, "enabled": True}
                pkg["enabled"] = True
            else:
                if cid not in entries:
                    raise Error(f"Skill is not selected: {skill}")
                affected = {cid}
                while True:
                    extra = {key for key, item in entries.items() if item["enabled"] and set(self.components(item["digest"], target)[key].get("requires", [])) & affected}
                    if extra <= affected:
                        break
                    affected |= extra
                if len(affected) > 1 and not cascade:
                    raise Error(f"Required by {', '.join(sorted(affected - {cid}))}; use --cascade")
                for key in affected:
                    entries[key]["enabled"] = False
            pkg["all"] = False

    def history(self, state):
        result = []
        generation = state["generation"]
        while generation:
            snapshot = read_json(self.generations / generation / "snapshot.json")
            result.append(snapshot)
            generation = snapshot["parent"]
        return result

    def rollback(self, state, targets, name, generation=None, skill=None, cascade=False):
        history = self.history(state)
        for target in targets:
            current = self.package(state, target, name)
            candidates = [h for h in history if h["generation"] == generation] if generation else history[1:]
            cid = "skill:" + skill if skill else None
            previous = None
            for h in candidates:
                candidate = h["packages"].get(target, {}).get(name)
                if candidate and (generation or (candidate["components"].get(cid) != current["components"].get(cid) if cid else candidate != current)):
                    previous = candidate
                    break
            if previous is None:
                raise Error(f"No prior matching generation for {name} on {target}")
            if not cid:
                state["packages"][target][name] = copy.deepcopy(previous)
                continue
            if cid not in previous["components"]:
                raise Error(f"Skill was not selected in that generation: {skill}")
            # A connected dependency cohort stays at a single release in both directions.
            # Resource-only dependencies can be shared by many skills and intentionally join the cohort.
            group = {cid}
            while True:
                expanded = set(group)
                for pkg in (current, previous):
                    for key, item in pkg["components"].items():
                        deps = set(self.components(item["digest"], target)[key].get("requires", []))
                        if key in group or deps & group:
                            expanded.add(key)
                            expanded |= deps
                if expanded == group:
                    break
                group = expanded
            if len(group) > 1 and not cascade:
                raise Error(f"Rollback requires dependency cohort {', '.join(sorted(group))}; use --cascade or roll back the plugin")
            for key in group:
                if key in previous["components"]:
                    current["components"][key] = copy.deepcopy(previous["components"][key])
                else:
                    current["components"].pop(key, None)
            current["all"] = False

    def check(self, state):
        for dest, target in state["links"].items():
            p = Path(dest)
            if not p.is_symlink() or os.readlink(p) != target:
                raise Error(f"Managed discovery entry was changed: {dest}")
        if state["generation"]:
            projection = self.generations / state["generation"] / "projection"
            if inventory(projection) != state["files"]:
                raise Error("Managed deployed content was modified; preserve your edits before updating")

    def render(self, state, generation):
        projection = self.generations / generation / "projection"
        projection.mkdir(parents=True)
        links = {}
        for target, packages in sorted(state["packages"].items()):
            for name, pkg in sorted(packages.items()):
                if not pkg["enabled"]:
                    continue
                entries = pkg["components"]
                for cid, item in sorted(entries.items()):
                    if not item["enabled"]:
                        continue
                    manifest = self.bundle(item["digest"])
                    variant = manifest["variants"][target]
                    c = self.components(item["digest"], target)[cid]
                    for dep in c.get("requires", []):
                        d = entries.get(dep)
                        if not d or not d["enabled"] or d["digest"] != item["digest"]:
                            raise Error(f"{target}/{name}/{cid} needs {dep} enabled at the same release")
                    source = self.store / item["digest"] / variant["root"] / c["path"]
                    dest = projection / target / name / c["path"]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        raise Error(f"Component path collision: {dest}")
                    if source.is_dir():
                        shutil.copytree(source, dest)
                    else:
                        shutil.copy2(source, dest)
                    if c["kind"] == "skill":
                        discovery = str(self.discovery(target) / cid.removeprefix("skill:"))
                        if discovery in links:
                            raise Error(f"Skill name collision at {discovery}")
                        links[discovery] = str(dest)
        return links, inventory(projection)

    def change_links(self, before, after):
        # Preflight the entire transaction before the first filesystem mutation.
        for name in sorted(set(before) | set(after)):
            p = Path(name)
            actual = os.readlink(p) if p.is_symlink() else None
            if (p.exists() or p.is_symlink()) and (not p.is_symlink() or actual not in {before.get(name), after.get(name)}):
                raise Error(f"Refusing to overwrite unmanaged or modified entry: {name}")
        for name in sorted(set(before) | set(after)):
            p = Path(name)
            if name in after:
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.parent / (".skillsctl-" + uuid.uuid4().hex)
                try:
                    tmp.symlink_to(after[name], target_is_directory=True)
                    os.replace(tmp, p)
                finally:
                    tmp.unlink(missing_ok=True)
            elif p.is_symlink():
                p.unlink()

    def recover(self):
        journal = self.root / "journal.json"
        if not journal.exists():
            return
        j = read_json(journal)
        committed = self.state()["generation"] == j["after"]["generation"]
        source, dest = (j["before"], j["after"]) if committed else (j["after"], j["before"])
        self.change_links(source["links"], dest["links"])
        atomic_json(self.root / "state.json", dest)
        journal.unlink()

    def commit(self, before, desired, dry_run=False):
        self.check(before)
        if before["packages"] == desired["packages"]:
            return {"generation": before["generation"], "dryRun": dry_run, "unchanged": True}
        generation = uuid.uuid4().hex
        links, files = self.render(desired, generation)
        after = copy.deepcopy(desired)
        after.update(generation=generation, parent=before["generation"], links=links, files=files)
        # Check collisions even for a dry run, without creating discovery directories.
        for name in links:
            p = Path(name)
            if (p.exists() or p.is_symlink()) and name not in before["links"]:
                raise Error(f"Unmanaged entry already exists: {name}")
        changes = {
            "generation": generation, "dryRun": dry_run,
            "added": sorted(set(links) - set(before["links"])),
            "removed": sorted(set(before["links"]) - set(links)),
            "retained": sorted(set(links) & set(before["links"])),
        }
        if dry_run:
            shutil.rmtree(self.generations / generation)
            return changes
        atomic_json(self.generations / generation / "snapshot.json", after)
        atomic_json(self.root / "journal.json", {"before": before, "after": after})
        try:
            self.change_links(before["links"], after["links"])
            atomic_json(self.root / "state.json", after)
        except BaseException:
            self.recover()
            raise
        (self.root / "journal.json").unlink()
        return changes
