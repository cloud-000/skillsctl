# Skill bundle contract v1

The producer and manager share this format, not implementation code. A bundle is
a directory or a gzip tar archive whose root contains `bundle.json`.

```json
{
  "schemaVersion": 1,
  "id": "example-plugin",
  "version": "1.0.0",
  "variants": {
    "codex": {
      "root": "bundles/codex",
      "components": [
        {
          "id": "skill:hello",
          "kind": "skill",
          "path": "skills/hello",
          "requires": []
        }
      ],
      "files": {
        "skills/hello/SKILL.md": {
          "sha256": "<64 lowercase hexadecimal characters>",
          "executable": false
        }
      }
    }
  }
}
```

- `schemaVersion` is exactly 1. Unsupported versions fail explicitly.
- `id` is lowercase kebab-case. `version` is a nonempty producer-assigned string;
  source resolution selects versions, rather than an implicit semver solver.
- `variants` has `codex`, `antigravity`, or both. There is no implicit fallback
  between target variants. Roots are contained, relative POSIX paths.
- Every variant file has a SHA-256 hash and executable flag in `files`. Added,
  deleted, modified, and permission-changed files invalidate the inventory.
- Each component has a stable `skill:<name>` or `resource:<name>` id, a `kind`,
  and a path relative to its variant root. Component paths may not overlap;
  every variant file must belong to exactly one component.
- A skill's id suffix, directory name, and `SKILL.md` frontmatter name agree.
  `name` must be a scalar kebab-case string of at most 64 characters, and
  `description` must be present. Full semantic YAML validation belongs to the
  producer/native harness validator.
- Resources contain supporting files, never nested `SKILL.md` entrypoints.
- `requires` contains component ids in the same variant. Cycles are permitted
  and handled as a closure. v1 requires dependencies at the same bundle digest.
  External services or plugins must be described by the producer; v1 does not
  auto-install external dependencies or resolve cross-package version ranges.
- No symlinks, devices, archive hardlinks, absolute paths, or `..` components.
- Optional `provenance`, `compatibility`, and other metadata are opaque to the
  manager. `status: "candidate"` explicitly blocks installation.
- Top-level reports may accompany the manifest; only variant components are
  deployed. SHA-256 of canonical JSON (sorted keys, indent 2, trailing newline)
  identifies the manifest and its bound file inventories in the store.

For native imports, `skillsctl pack` generates this representation without
modifying instruction content. For stronger compatibility claims, a producer
should perform harness validation, preserve licenses, and record its input commit
and transformation revision before publishing immutable artifacts.
