# skillsctl

A small Python 3.11.8+ CLI for installing, updating, enabling, disabling, removing,
and rolling back skill bundles independently for Codex and Antigravity. No runtime
dependencies. macOS and Linux are supported; the transaction lock uses `flock`.

skillsctl manages **content already adapted for its target harness**. It does not
rewrite prompts, choose model mappings, fetch pstack upstream, or import
pstack-portable. A plugin is a managed package of skills and supporting resources.

## Run

Use `./bin/skillsctl`, add `bin/` to PATH, or install with `pip install .` in your
preferred environment. All commands emit JSON; errors go to stderr with exit code 1.

```sh
skillsctl install ~/CodeProjects/lab/plugins/pstack-portable/dist/bundle \
  --harness codex --harness antigravity

skillsctl list
skillsctl disable pstack-portable --harness codex --skill sharpen
skillsctl enable pstack-portable --harness codex --skill sharpen
skillsctl disable pstack-portable --harness antigravity
skillsctl enable pstack-portable --harness antigravity
skillsctl update pstack-portable --harness codex --harness antigravity
skillsctl history
skillsctl rollback pstack-portable --harness codex
skillsctl doctor
```

When a skill is required by other enabled skills, disabling it fails with the list
of dependents. Add `--cascade` to disable those dependents as well. Re-enabling a
plugin restores its prior per-skill preferences, not all skills indiscriminately.

Use an isolated home to try commands without touching your active configuration:

```sh
skillsctl --home /tmp/skillsctl-demo install /path/to/bundle --harness codex
skillsctl --home /tmp/skillsctl-demo doctor
```

`--home` changes both harness destinations and the default state directory.
`--state-dir` (or `SKILLSCTL_STATE_DIR`) overrides only the manager's state location.
Use one state directory per harness home. `--dry-run` validates and plans changes
without changing active skills or desired state; it can populate the download store.

## Sources and individual skills

Supported inputs:

- A directory containing `bundle.json` (the normative format).
- A standalone directory containing `SKILL.md` and its resources.
- A native skills-only plugin containing `plugin.json` or a supported compatibility
  manifest and conventional `skills/<name>/SKILL.md` directories.
- A Git source: `git+https://host/owner/repo.git#ref:subdirectory`.
- An HTTPS `.tar.gz` release, with `--sha256 <trusted-release-digest>`.

```sh
skillsctl install ./my-skill --harness codex
skillsctl pack ./my-plugin ./my-bundle
skillsctl validate ./my-bundle
skillsctl install ./my-bundle --harness antigravity --skill alpha
skillsctl update my-plugin --harness antigravity --source ./new-bundle
skillsctl remove my-plugin --harness antigravity --skill alpha
skillsctl remove my-plugin --harness antigravity
```

Installing selected skills also installs their declared required dependencies.
Native imports cannot infer arbitrary prose-level dependencies; provide a bundle
manifest when cross-skill behavior depends on other skills. No native plugin
install hooks are run. Native plugins containing MCP servers, lifecycle hooks,
rules, registered agents, or apps are rejected rather than silently downgraded.
The v1 backend manages **skills and resources**, not those native runtime services.

The source is saved for `update`; updates resolve that source again. A Git SHA or
versioned URL remains pinned until `--source` changes it. For a newly published
HTTPS archive, provide its new URL and checksum. A local producer's `dist/bundle`
can remain a stable source path while its immutable contents change.

## Activation and isolation

- Codex discovery: `~/.agents/skills/<skill-name>`.
- Antigravity discovery: `~/.gemini/config/skills/<skill-name>`.
- Discovery entries are owned symlinks into a generation's package tree. The tree
  preserves relative references and includes only enabled selected components.
- Both harnesses use `.agents/skills` for workspace discovery. v1 explicitly
  rejects `--workspace` instead of pretending that shared paths provide isolation.
- A new harness session/reload may be needed after changes. Already-loaded prompt
  content cannot be revoked from a running conversation.

Disabling removes active discovery and deployment, not the immutable historical
store. This is activation management, not a filesystem access-control boundary.
Other independently installed copies are not disabled by this tool. Names collide
explicitly; skillsctl never overwrites an unmanaged skill directory.

## Rollback and transactions

State lives under `~/.local/share/skillsctl/`:

```text
store/<manifest-sha256>/           verified immutable bundle snapshots
generations/<id>/projection/       selected components preserving package layout
generations/<id>/snapshot.json     version pins, preferences, paths, and hashes
state.json                        current generation and desired state
journal.json                      present only during an unfinished transaction
lock                              serializes cooperating skillsctl processes
```

Each successful change is a generation. `history` lists exact generation IDs.
Rollback restores the most recent different package state, including activation
preferences, without network access. Select an exact historical snapshot with
`--generation <id>`.

```sh
skillsctl rollback my-plugin --harness codex --generation <id>
skillsctl rollback my-plugin --harness codex --skill alpha
```

An individual skill rollback changes only that skill when it is independent.
Connected required dependencies and dependents form a rollback cohort. For a
cohort, the command explains the affected components and requires `--cascade`.
This avoids combining a skill with incompatible shared resources. pstack shares
resources extensively, so whole-package rollback is usually the useful choice.

The journal recovers interrupted processes on the next command: an uncommitted
transaction restores previous links; a committed one completes its links. Changes
across multiple discovery entries are recoverable, not simultaneously visible.
Store and deployed-content hashes detect local edits; changed managed content or
links cause a refusal, not an overwrite. Move intentional edits into a source
bundle and restore the recorded file before applying another change. There is no
automatic garbage collection; old snapshots remain available for offline rollback.

Checksums provide integrity, not publisher authentication. Get release digests
through a trusted channel. Do not run two state directories against the same
harness home, or edit discovery entries during a deployment.

## Contract and tests

See [BUNDLE_FORMAT.md](BUNDLE_FORMAT.md). The format is independent of any producer.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests cover unrelated standalone skills and plugins, both harnesses, partial
selection, disable/enable, dependency conflicts, update, individual and package
rollback, unmanaged collisions, tampering, dry runs, and crash recovery. `doctor`
checks filesystem integrity, not host discovery or the quality of skill behavior.
