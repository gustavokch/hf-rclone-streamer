# PR #5 Remediation: no-mount rclone fallback must not override explicit binaries

## Goal

`--no-mount` falls back to plain `rclone` when `rclone-fuse` is absent — but only
for the **default** binary name. An explicit `--rclone-binary` / env / YAML choice
must fail loud via `RcloneNotFoundError`, not be silently swapped.

## Architecture

Single gate change in `build_config()` (hf_rclone_streamer.py), before
`set_rclone_binary()`. Config default is `"rclone-fuse"` (config.py:39).

## Tasks

### T1: Fallback only fires for the default binary name

- Modify: `test_cli.py` (new test), `hf_rclone_streamer.py` (gate)
- Test first (Red): `test_build_config_no_mount_respects_explicit_binary` —
  `--no-mount --rclone-binary rclone-custom` with `rclone-custom` missing and
  `rclone` present must keep `rclone-custom`.
- Implement (Green): change gate to
  `if binary == "rclone-fuse" and not shutil.which(binary) and shutil.which("rclone"):`
- Verify: `.venv/bin/python test_cli.py` (17 passed), then full suite.
- Commit: `fix(cli): no-mount fallback only applies to the default rclone-fuse name` — done (c81817a)

## Result

- Red: `test_build_config_no_mount_respects_explicit_binary` → `AssertionError: binary: 'rclone'`
- Green: `test_cli.py` → 17 passed
