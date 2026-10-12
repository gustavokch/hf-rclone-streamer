# PR #3 Remediation Plan — YAML config + GGUF quant

**Date:** 2026-10-12
**Branch:** `feat/yaml-config-gguf-quant` (PR #3)
**Goal:** Resolve the review findings posted at
https://github.com/gustavokch/hf-rclone-streamer/pull/3#issuecomment-6131745510
without changing PR behavior otherwise.
**Architecture:** single-package Python CLI; config layering DEFAULTS < YAML < env < CLI.
**Tech stack:** Python 3.9+, argparse, PyYAML; tests are plain-assert scripts run via `.venv/bin/python test_*.py` (no pytest, no CI).
**Spec reference:** PR #3 body + review comment above (no SPEC.md in repo).

## Task status

- [x] T1 Warn on legacy `config.json` when default `config.yaml` absent
- [x] T2 Warn when an explicit `--config` path does not exist
- [x] T3 `default_model_id` property: null-guard + drop duplicated literal
- [x] T4 Parser help text: single source for default repo ID
- [x] T5 Extract `build_config(args)` seam; test `--quant` / `--config` wiring
- [x] T5b Fix precedence: non-None argparse defaults clobber YAML/env (🔴, see addendum comment)
- [x] T6 README: tab fix + precise `--quant` case wording
- [x] T7 Full suite green + push to `origin/feat/yaml-config-gguf-quant`

Rules: red → confirm fail → green → confirm pass → atomic commit, per task.
One task in progress at a time.

---

## T1 — Warn on legacy `config.json`

**Target files:** Modify `config.py`; Test `test_config.py`
**Consumes:** `Config._load_from_file`, `Config.config_dir`
**Produces:** warning text containing `config.json` and a rename hint

**Step 1 — failing test** (append to `test_config.py`):

```python
def test_legacy_json_config_warns():
    """config.json beside a missing config.yaml ⇒ warn (settings silently lost otherwise)."""
    import warnings
    with tempfile.TemporaryDirectory() as td:
        xdg = Path(td) / "xdg"
        (xdg / "hf-rclone-streamer").mkdir(parents=True)
        (xdg / "hf-rclone-streamer" / "config.json").write_text('{"dest_dir": "/Legacy"}')
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                cfg = Config()
    assert any("config.json" in str(w.message) for w in caught), [
        str(w.message) for w in caught
    ]
    assert cfg.dest_dir == "/Models", f"dest_dir: {cfg.dest_dir!r}"
```

**Step 2:** `.venv/bin/python test_config.py` → new test FAILS (no warning).
**Step 3 — implementation** (`config.py`): add `import warnings` at module top.
In `_load_from_file`, remember whether the path was explicit; when the default
YAML path is missing and `self.config_dir / "config.json"` exists, emit

```python
warnings.warn(
    f"Legacy config file {legacy} is no longer read; rename it to "
    f"{legacy.with_suffix('.yaml')} to keep your settings"
)
```

before `return`.
**Step 4:** `.venv/bin/python test_config.py` → all PASS.
**Step 5:** `git add config.py test_config.py && git commit -m "fix(config): warn when legacy config.json is ignored"`

---

## T2 — Warn on missing explicit `--config` path

**Target files:** Modify `config.py`; Test `test_config.py`
**Consumes:** `Config.__init__(config_file=...)` explicit-path branch
**Produces:** warning containing the missing path

**Step 1 — failing test** (append):

```python
def test_missing_explicit_config_warns():
    """An explicit --config path that does not exist must not fail silently."""
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Config(config_file=Path("/nonexistent/nope.yaml"))
    assert any("nope.yaml" in str(w.message) for w in caught), [
        str(w.message) for w in caught
    ]
```

**Step 2:** run `test_config.py` → FAILS.
**Step 3 — implementation:** in `_load_from_file`, on `not config_file.exists()`:
warn when the caller passed an explicit path (`config_file is not None` at
entry); stay silent for the default path (missing default file is normal).
**Step 4:** run → PASS. Existing `test_new_gguf_defaults` (explicit missing path)
still passes — warnings are not errors.
**Step 5:** `git commit -m "fix(config): warn when an explicit config file is missing"`

---

## T3 — Null-guard `default_model_id`, dedupe literal

**Target files:** Modify `config.py`; Test `test_config.py`
**Consumes:** YAML key `default_model_id` (may be `null`)
**Produces:** always-str `Config.default_model_id`

**Step 1 — failing test** (append):

```python
def test_default_model_id_null_falls_back():
    """`default_model_id: null` in YAML stores None (key exists) — property must fall back."""
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(Path(td), "default_model_id:\n")
        cfg = Config(config_file=path)
    assert cfg.default_model_id == (
        "huihui-ai/Huihui-Qwen3.8-Flash-Next-abliterated-v2-GGUF"
    ), f"default_model_id: {cfg.default_model_id!r}"
```

**Step 2:** run → FAILS (`None` returned).
**Step 3 — implementation:** change the property to
`return self.get("default_model_id") or self.DEFAULTS["default_model_id"]`
(also removes the duplicated literal at `config.py:180`).
**Step 4:** run → PASS.
**Step 5:** `git commit -m "fix(config): fall back when default_model_id is null" `

---

## T4 — Single source for default repo in parser help

**Target files:** Modify `hf_rclone_streamer.py`; Test `test_cli.py`
**Consumes:** `Config.DEFAULTS["default_model_id"]`
**Produces:** help text derived from `DEFAULTS`

**Step 1 — failing test** (append to `test_cli.py`):

```python
def test_help_shows_configured_default_model():
    from config import Config
    assert Config.DEFAULTS["default_model_id"] in create_parser().format_help()
```

(Also asserts help text references the *live* default, not a stale literal —
this passes today only by coincidence of the literal matching; the point of T4
is removing the third copy.)

**Step 2:** run `test_cli.py` → passes trivially; verify the *fix* direction by
the T4 edit below, then re-run.
**Step 3 — implementation:** in `create_parser()`, replace the two-part literal

```python
help="Model ID (default: huihui-ai/"
"Huihui-Qwen3.8-Flash-Next-abliterated-v2-GGUF)",
```

with `help=f"Model ID (default: {Config.DEFAULTS['default_model_id']})"`.
**Step 4:** `.venv/bin/python test_cli.py` → PASS.
**Step 5:** `git commit -m "refactor(cli): derive default model help from Config.DEFAULTS"`

---

## T5 — Extract `build_config(args)` seam; test wiring

**Target files:** Modify `hf_rclone_streamer.py`; Test `test_cli.py`
**Consumes:** `main()`'s config-building block (`hf_rclone_streamer.py:670-701`)
**Produces:** `build_config(args) -> Config` (pure except env/file reads)

**Step 1 — failing tests** (append to `test_cli.py`):

```python
from unittest import mock
from hf_rclone_streamer import build_config  # fails: ImportError (red)

def test_build_config_quant_flag_sets_config():
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}, clear=True):
            args = create_parser().parse_args(["download", "--quant", "q4_k_xl"])
            cfg = build_config(args)
    assert cfg.quant == "q4_k_xl", f"quant: {cfg.quant!r}"

def test_build_config_explicit_yaml_loaded():
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "c.yaml"
        cfg_path.write_text("dest_dir: /Models/X\n")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}, clear=True):
            args = create_parser().parse_args(["--config", str(cfg_path), "download"])
            cfg = build_config(args)
    assert cfg.dest_dir == "/Models/X", f"dest_dir: {cfg.dest_dir!r}"
```

(`test_cli.py` needs `import tempfile` and `from pathlib import Path`.)

**Step 2:** run → ImportError / FAIL (red).
**Step 3 — implementation:** move the `Config(...)` construction and every
`config.set(...)` / `getattr(args, "quant", ...)` call from `main()` into a new
module-level `build_config(args)`; `main()` calls it, then continues with
`set_rclone_binary(config.rclone_binary)` and dispatch. No behavior change.
**Step 4:** run `test_cli.py` → PASS.
**Step 5:** `git commit -m "refactor(cli): extract build_config seam and test flag wiring"`

---

## T5b — Fix precedence: argparse defaults clobber YAML/env (🔴)

**Target files:** Modify `hf_rclone_streamer.py`; Test `test_cli.py`
**Consumes:** `create_parser` non-None defaults, `build_config` truthiness checks
**Produces:** documented precedence DEFAULTS < YAML < env < CLI actually enforced
for `cache_dir`, `dest_dir`, `include`, `aria2c_connections`, `drive_chunk_size`

**Step 1 — failing tests** (append to `test_cli.py`; note
`test_build_config_explicit_yaml_loaded` from T5 is already red):

```python
def test_build_config_env_dest_dir_survives():
    """HF_RCLONE_DEST_DIR must survive when the --dest-dir flag is absent."""
    from hf_rclone_streamer import build_config
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": td, "HF_RCLONE_DEST_DIR": "/EnvWins"},
            clear=True,
        ):
            args = create_parser().parse_args(["download"])
            cfg = build_config(args)
    assert cfg.dest_dir == "/EnvWins", f"dest_dir: {cfg.dest_dir!r}"


def test_build_config_cli_dest_dir_wins_over_yaml():
    """An explicit --dest-dir flag must override the YAML file."""
    from hf_rclone_streamer import build_config
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "c.yaml"
        cfg_path.write_text("dest_dir: /Models/X\n")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}, clear=True):
            args = create_parser().parse_args(
                ["--config", str(cfg_path), "--dest-dir", "/CliWins", "download"]
            )
            cfg = build_config(args)
    assert cfg.dest_dir == "/CliWins", f"dest_dir: {cfg.dest_dir!r}"
```

**Step 2:** `.venv/bin/python test_cli.py` → env test FAILS, explicit-yaml
test FAILS (red confirmed).
**Step 3 — implementation:** in `create_parser()` set `default=None` on
`--cache-dir`, `--dest-dir`, `--include`, `--aria2c-connections`,
`--drive-chunk-size` (help text already documents the real defaults); in
`build_config()` change those five checks to `if args.X is not None:`.
Booleans stay truthy-checked (`store_true` defaults False — no clobber).
**Step 4:** run → all PASS.
**Step 5:** `git commit -m "fix(cli): stop argparse defaults from clobbering YAML/env config"`

---

## T6 — README fixes

**Target files:** Modify `README.md` (docs only, no tests)

**Step 1 — no test (docs).**
**Step 2 — edit:**
1. Line 68: replace the leading literal tab with 4 spaces (match sibling lines).
2. Line 184: reword `(case-insensitive; overrides --include)` →
   `(the tag is upper-cased before matching; overrides --include)` — POSIX
   `fnmatch` on filenames is case-sensitive (`hf_api.py:467`).
**Step 3:** `grep -n "$(printf '\t')" README.md` → empty; visual check.
**Step 4:** n/a
**Step 5:** `git commit -m "docs(readme): fix tab indent, describe quant tag normalization"`

---

## T7 — Verify & push

1. Run every suite: `for t in test_*.py; do .venv/bin/python $t || break; done` —
   must be 100% green (gate).
2. `git push origin feat/yaml-config-gguf-quant`.
3. Report: resolved findings, test counts, PR link.
