# Changelog

All notable changes to Osprey are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-01

First stable release. Osprey started as an internal prototype ("PepperMint Grep") and was
rebuilt into a PowerGrep-style, cross-platform search-and-replace GUI backed by `ripgrep`/`grep`.

### Added

- Multi-engine search backend with automatic detection and manual switching between `ripgrep`
  and `grep`.
- Multi-folder search with include/exclude file filters supporting both glob and regex syntax,
  plus an instant, debounced fuzzy filter over already-returned results.
- Search options: plain text or regex, case sensitivity, whole-word matching, and a
  file-names-only ("Find Files") mode.
- Results panel grouped by file with expandable matched lines, backed by a `QTreeView` model
  for large result sets.
- Live preview pane with match highlighting, line-number display, and line-wrap toggle;
  clicking a result scrolls the preview to the matched line.
- Configurable external editor integration: open the matched file (and line, when available)
  in any user-defined editor from the result or preview context menu.
- Line-, file-, and project-level replace with an inline diff preview (color-coded added and
  removed text) before any change is written, plus an optional pre-replace backup and an undo
  path via replace session history.
- Save/load search options (`.opq`), save/load full result snapshots (`.opr`), and
  export/import of include/exclude rule sets.
- Recent menu tracking the last 20 search query profiles and the last 20 result snapshots.
- Search history dropdown with the last 20 used patterns.
- Two-level configuration: global settings plus an optional project-level `.osprey/settings.json`
  override.
- Command-line interface: positional `FILE_OR_PATH` shortcut that infers whether to load a
  `.opq` profile, a `.opr` snapshot, or set the search directory; `--headless` mode with
  optional `--json` output for scripting.
- Cross-platform packaging, producing a single portable executable for Windows, macOS, and Linux with no Python runtime required.
- Application branding (name, icon, taskbar/dock identity) unified under the Osprey name.

[1.0.0]: https://github.com/petercai/osprey/releases/tag/v1.0.0
