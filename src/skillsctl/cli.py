from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

from .bundle import Error, TARGETS, normalize, validate
from .manager import Manager


def parser():
    p = argparse.ArgumentParser(description="Manage versioned skill bundles independently for each harness.")
    p.add_argument("--home", type=Path, help="Harness home (default: current home; useful for isolated testing)")
    p.add_argument("--state-dir", type=Path, default=os.environ.get("SKILLSCTL_STATE_DIR"))
    commands = p.add_subparsers(dest="command", required=True)
    for command in ("install", "update", "enable", "disable", "remove", "rollback"):
        s = commands.add_parser(command)
        s.add_argument("subject", help="Source for install; package id for other commands")
        s.add_argument("--harness", action="append", choices=TARGETS, required=True)
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--workspace", type=Path, help="Workspace isolation is not yet supported; fails explicitly")
        if command == "install":
            s.add_argument("--skill", action="append", help="Select skills (dependencies included); default: all")
            s.add_argument("--sha256")
        if command == "update":
            s.add_argument("--source", help="Override the saved source")
            s.add_argument("--sha256")
        if command in ("enable", "disable", "rollback", "remove"):
            s.add_argument("--skill")
            s.add_argument("--cascade", action="store_true")
        if command == "rollback":
            s.add_argument("--generation")
    for command in ("list", "history", "doctor"):
        commands.add_parser(command)
    s = commands.add_parser("validate")
    s.add_argument("path", type=Path)
    s = commands.add_parser("pack")
    s.add_argument("source", type=Path)
    s.add_argument("destination", type=Path)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "validate":
            m = validate(args.path)
            print(json.dumps({"id": m["id"], "version": m["version"], "variants": list(m["variants"])}))
            return 0
        if args.command == "pack":
            if args.destination.exists():
                raise Error("Pack destination already exists")
            m = normalize(args.source, args.destination)
            print(json.dumps({"id": m["id"], "path": str(args.destination)}))
            return 0
        manager = Manager(args.home, args.state_dir)
        with manager.locked():
            before = manager.state()
            if args.command == "list":
                output = copy.deepcopy(before["packages"])
                for target, packages in output.items():
                    for pkg in packages.values():
                        pkg["version"] = manager.bundle(pkg["digest"])["version"]
                        for item in pkg["components"].values():
                            item["version"] = manager.bundle(item["digest"])["version"]
                print(json.dumps(output, indent=2))
                return 0
            if args.command == "history":
                print(json.dumps([{k: h[k] for k in ("generation", "parent", "packages")} for h in manager.history(before)], indent=2))
                return 0
            if args.command == "doctor":
                manager.check(before)
                for packages in before["packages"].values():
                    for pkg in packages.values():
                        manager.bundle(pkg["digest"])
                        for item in pkg["components"].values():
                            manager.bundle(item["digest"])
                print(json.dumps({"ok": True, "generation": before["generation"], "note": "Filesystem integrity verified; host discovery and runtime behavior require a new-session smoke test."}))
                return 0
            if args.workspace:
                raise Error("Both harnesses share .agents/skills in workspaces. Independent workspace activation is not supported; use global scope.")
            targets = sorted(set(args.harness))
            desired = copy.deepcopy(before)
            if args.command == "install":
                source = str(Path(args.subject).expanduser().resolve()) if "://" not in args.subject else args.subject
                manager.install(desired, source, targets, args.skill, args.sha256)
            elif args.command == "update":
                for target in targets:
                    pkg = manager.package(desired, target, args.subject)
                    source = args.source or pkg["source"]
                    if "://" not in source:
                        source = str(Path(source).expanduser().resolve())
                    checksum = args.sha256 or (pkg["checksum"] if not args.source else None)
                    manager.install(desired, source, [target], checksum=checksum, update=args.subject)
            elif args.command in ("enable", "disable"):
                manager.toggle(desired, targets, args.subject, args.command == "enable", args.skill, args.cascade)
            elif args.command == "remove":
                if args.skill:
                    manager.toggle(desired, targets, args.subject, False, args.skill, args.cascade)
                    for target in targets:
                        pkg = manager.package(desired, target, args.subject)
                        pkg["components"].pop("skill:" + args.skill)
                else:
                    for target in targets:
                        manager.package(desired, target, args.subject)
                        del desired["packages"][target][args.subject]
            elif args.command == "rollback":
                manager.rollback(desired, targets, args.subject, args.generation, args.skill, args.cascade)
            result = manager.commit(before, desired, args.dry_run)
            result["note"] = "Reload/start a new harness session to refresh discovered skills."
            print(json.dumps(result, indent=2))
        return 0
    except (Error, OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"skillsctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
