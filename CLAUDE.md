# CLAUDE.md

## Architecture diagram (keep in sync)

`docs/architecture/snapshotkit-architecture.html` is generated with the archify skill from
`docs/architecture/snapshotkit.architecture.json`. This rule applies to this repository only.

Whenever a change touches components, data flows, trust boundaries or deployment (anything
under `src/snapshotkit/`, `templates/`, `deploy/`, `docker/`), update the diagram in the same
piece of work:

1. Commit the code change first. Archify checks `sources` line ranges against committed bytes
   at `meta.repository.revision`, not the working tree.
2. In the JSON, set `meta.repository.revision` to that commit (`git rev-parse HEAD`), fix any
   `sources` line ranges that moved, and add, remove or relabel components and connections to
   match the code.
3. Regenerate from the repo root:
   `node ~/.claude/skills/archify/bin/archify.mjs finalize architecture docs/architecture/snapshotkit.architecture.json docs/architecture/snapshotkit-architecture.html --repo-root . --quality showcase --json`
   It must exit 0 with `"status":"pass"`. If it fails, follow the archify skill's repair steps.
4. Commit the JSON and HTML together (`docs: update architecture diagram`). The receipts
   (`*.finalize*.json`, `*.browser-check.json`, `*.delivery.json`) are git-ignored.

A change that only touches tests, docs or comments doesn't need a diagram update.
