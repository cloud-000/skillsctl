"""Bundle v1 validation and source normalization. No content transformations."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import subprocess
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath


class Error(Exception):
    pass


NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
TARGETS = ("codex", "antigravity")


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (ValueError, OSError) as exc:
        raise Error(f"Cannot read JSON {path}: {exc}") from exc


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise Error(f"Invalid relative path: {value!r}")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("", ".", "..") for x in value.split("/")):
        raise Error(f"Unsafe relative path: {value!r}")
    return p


def inventory(root):
    result = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_symlink():
            raise Error(f"Bundle symlinks are not allowed: {p}")
        if p.is_file():
            result[p.relative_to(root).as_posix()] = {
                "sha256": sha(p.read_bytes()),
                "executable": bool(p.stat().st_mode & 0o111),
            }
        elif not p.is_dir():
            raise Error(f"Unsupported filesystem entry: {p}")
    return result


def skill_name(path):
    text = path.read_text()
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        raise Error(f"Missing skill frontmatter: {path}")
    front = match[1]
    name = re.search(r"^name:\s*([^\n]+)$", front, re.M)
    desc = re.search(r"^description:\s*\S", front, re.M)
    value = name[1].strip().strip("\"'") if name else ""
    if not NAME.fullmatch(value) or len(value) > 64 or not desc:
        raise Error(f"Invalid skill name/description: {path}")
    return value


def validate(root):
    root = Path(root)
    m = read_json(root / "bundle.json")
    if not isinstance(m, dict) or m.get("schemaVersion") != 1:
        raise Error("Unsupported bundle schemaVersion (expected 1)")
    if m.get("status") == "candidate":
        raise Error("Candidate bundles are for inspection only; install a reviewed release")
    if not isinstance(m.get("id"), str) or not NAME.fullmatch(m["id"]):
        raise Error("Bundle id must be lowercase kebab-case")
    if not isinstance(m.get("version"), str) or not m["version"]:
        raise Error("Bundle version is required")
    if not isinstance(m.get("variants"), dict) or not m["variants"]:
        raise Error("Bundle variants are required")
    roots = []
    for target, v in m["variants"].items():
        if target not in TARGETS or not isinstance(v, dict):
            raise Error(f"Unsupported target: {target}")
        base = root / relative(v.get("root"))
        roots.append(v["root"])
        if not base.is_dir() or base.is_symlink():
            raise Error(f"Missing variant root: {base}")
        actual = inventory(base)
        if actual != v.get("files"):
            raise Error(f"Checksum/file inventory mismatch: {target}")
        components = v.get("components")
        if not isinstance(components, list) or not components:
            raise Error(f"Missing components: {target}")
        ids, paths, names = set(), [], set()
        for c in components:
            if not isinstance(c, dict) or c.get("kind") not in ("skill", "resource"):
                raise Error("v1 supports skill and resource components only")
            cid = c.get("id", "")
            if not isinstance(cid, str) or not re.fullmatch(r"(?:skill|resource):[a-z0-9]+(?:-[a-z0-9]+)*", cid):
                raise Error(f"Invalid component id: {cid}")
            if not cid.startswith(c["kind"] + ":") or cid in ids:
                raise Error(f"Duplicate or mismatched component id: {cid}")
            ids.add(cid)
            path = relative(c.get("path"))
            if any(path == p or path in p.parents or p in path.parents for p in paths):
                raise Error(f"Overlapping component path: {path}")
            paths.append(path)
            if not (base / path).exists():
                raise Error(f"Missing component: {path}")
            if c["kind"] == "skill":
                n = skill_name(base / path / "SKILL.md")
                if n in names or n != path.name or cid != "skill:" + n:
                    raise Error(f"Skill id, name and folder must agree: {cid}")
                names.add(n)
            elif path.name == "SKILL.md" or any(p.name == "SKILL.md" for p in (base / path).rglob("SKILL.md")):
                raise Error("Resources cannot hide discoverable skills")
            if not isinstance(c.get("requires", []), list):
                raise Error(f"Invalid requires: {cid}")
        for c in components:
            if any(not isinstance(dep, str) or dep not in ids for dep in c.get("requires", [])):
                raise Error(f"Unknown dependency in {c['id']}")
        for file in actual:
            p = PurePosixPath(file)
            if not any(p == x or x in p.parents for x in paths):
                raise Error(f"Unowned variant file: {target}/{file}")
    for a in roots:
        for b in roots:
            if a != b and PurePosixPath(a) in PurePosixPath(b).parents:
                raise Error("Variant roots may not overlap")
    return m


def normalize(source, dest, targets=TARGETS):
    """Normalize a plain skill or skills-only native plugin without rewriting it."""
    source, dest = Path(source), Path(dest)
    if (source / "bundle.json").exists():
        validate(source)
        # Reject symlinks even outside the variant tree before copying.
        inventory(source)
        shutil.copytree(source, dest, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
        return validate(dest)
    native = next((source / p for p in ("plugin.json", ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json", ".claude-plugin/plugin.json") if (source / p).is_file()), None)
    if (source / "SKILL.md").is_file():
        name = skill_name(source / "SKILL.md")
        version = "local"
        skill_dirs = [source]
        standalone = True
    elif native:
        meta = read_json(native)
        name, version = meta.get("name"), str(meta.get("version", "local"))
        if any((source / x).exists() for x in ("mcp.json", ".mcp.json", "hooks", "agents", "rules", ".app.json")) or any(meta.get(k) for k in ("mcpServers", "hooks", "agents", "rules", "apps", "extensions")):
            raise Error("Native plugin has non-skill runtime components; provide an explicitly adapted bundle. No components were silently omitted.")
        if meta.get("skills", "./skills/").rstrip("/") not in ("./skills", "skills"):
            raise Error("Custom native skill paths require a bundle.json")
        skill_dirs = sorted(p.parent for p in (source / "skills").glob("*/SKILL.md"))
        standalone = False
    else:
        raise Error("Expected bundle.json, SKILL.md, or a skills-only plugin manifest")
    if not name or not NAME.fullmatch(name) or not skill_dirs:
        raise Error("Invalid package name or empty skills package")
    inventory(source)
    m = {"schemaVersion": 1, "id": name, "version": version, "variants": {}}
    for target in targets:
        base = dest / "bundles" / target
        components = []
        if standalone:
            shutil.copytree(source, base / "skills" / name, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
        else:
            shutil.copytree(source, base, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
        for p in skill_dirs:
            n = skill_name(p / "SKILL.md")
            if p.name != n and not standalone:
                raise Error(f"Skill folder must match name: {p}")
            components.append({"id": "skill:" + n, "kind": "skill", "path": "skills/" + n, "requires": []})
        if not standalone:
            for p in sorted(base.iterdir()):
                if p.name == "skills":
                    continue
                rid = "resource:extra-" + str(len(components))
                components.append({"id": rid, "kind": "resource", "path": p.name, "requires": []})
                for c in components:
                    if c["kind"] == "skill":
                        c["requires"].append(rid)
        m["variants"][target] = {"root": f"bundles/{target}", "components": components, "files": inventory(base)}
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "bundle.json").write_bytes(canonical(m))
    return validate(dest)


def fetch(source, directory, expected_sha=None):
    """Fetch local folders, HTTPS tarballs, or git+HTTPS#ref:subdir sources."""
    directory = Path(directory)
    if source.startswith("git+https://"):
        url, sep, fragment = source[4:].partition("#")
        ref, _, subdir = fragment.partition(":")
        if not sep or not ref or ref.startswith("-"):
            raise Error("Git source syntax: git+https://host/repo.git#ref:subdir")
        repo = directory / "checkout"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "fetch", "--depth=1", url, ref], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", "-q", "FETCH_HEAD"], check=True)
        return repo / relative(subdir) if subdir else repo
    if source.startswith("https://"):
        if not expected_sha or not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
            raise Error("HTTPS archives require --sha256 with the trusted release digest")
        archive = directory / "download.tar.gz"
        with urllib.request.urlopen(source, timeout=60) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
        if sha(archive.read_bytes()) != expected_sha:
            raise Error("Archive checksum mismatch")
        unpack = directory / "unpacked"
        unpack.mkdir()
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                relative(member.name.rstrip("/"))
                if not (member.isfile() or member.isdir()) or member.mode & (stat.S_ISUID | stat.S_ISGID):
                    raise Error(f"Unsafe archive entry: {member.name}")
            tar.extractall(unpack, filter="data")
        if (unpack / "bundle.json").exists():
            return unpack
        children = list(unpack.iterdir())
        if len(children) == 1 and children[0].is_dir():
            return children[0]
        raise Error("Archive must contain one bundle root")
    p = Path(source).expanduser().resolve()
    if not p.is_dir():
        raise Error(f"Source directory does not exist: {p}")
    return p
