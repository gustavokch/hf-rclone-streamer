"""Unit tests for hf_api.get_model_info file listing (mocked, no network).

Seam under test: ``get_model_info`` must list the repo *recursively*, so
files in subfolders (e.g. GGUF repos that organize quant shards under
``unsloth-UD-Q4_K_XL/``) are seen by the downloader, and include/exclude
patterns must still apply to the full relative path.

The fake ``list_repo_tree`` behaves like the real one: without
``recursive=True`` it yields only top-level entries.

Run: .venv/bin/python test_hf_api.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hf_api
from hf_api import get_model_info
from huggingface_hub.hf_api import RepoFile, RepoFolder

SHARD_1 = "unsloth-UD-Q4_K_XL/Qwen-Q4_K_XL-00001-of-00002.gguf"
SHARD_2 = "unsloth-UD-Q4_K_XL/Qwen-Q4_K_XL-00002-of-00002.gguf"


def _fake_list_repo_tree(**kwargs):
    items = [
        RepoFolder(path="unsloth-UD-Q4_K_XL", oid="tree0"),
        RepoFile(path="README.md", size=10, oid="blob1", lfs=None),
        RepoFile(path="mmproj-BF16.gguf", size=50, oid="blob2", lfs=None),
    ]
    if kwargs.get("recursive"):
        items += [
            RepoFile(path=SHARD_1, size=100, oid="blob3", lfs=None),
            RepoFile(path=SHARD_2, size=200, oid="blob4", lfs=None),
        ]
    return items


def _run(include=None, exclude=None):
    api_instance = mock.MagicMock()
    api_instance.model_info.return_value = mock.MagicMock(
        author="huihui-ai", tags=["gguf"], card_data={}
    )
    with mock.patch.object(hf_api, "HfApi", return_value=api_instance), \
            mock.patch.object(
                hf_api, "list_repo_tree", side_effect=_fake_list_repo_tree
            ):
        return get_model_info(
            "huihui-ai/fake-repo",
            include_patterns=include,
            exclude_patterns=exclude,
        )


def test_subfolder_files_are_listed():
    info = _run()
    paths = [f.path for f in info.files]
    assert SHARD_1 in paths, f"subfolder shard missing: {paths}"
    assert SHARD_2 in paths, f"subfolder shard missing: {paths}"
    # Folder entries are skipped, top-level files kept
    assert "README.md" in paths, paths
    assert len(paths) == 4, paths


def test_total_size_counts_subfolder_files():
    info = _run()
    assert info.total_size == 360, f"total_size: {info.total_size}"


def test_include_pattern_matches_subfolder_paths():
    info = _run(include=["*Q4_K_XL*.gguf"])
    paths = [f.path for f in info.files]
    assert paths == [SHARD_1, SHARD_2], paths


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
