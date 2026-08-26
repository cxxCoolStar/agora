import hashlib

import pytest

from agora.tools import ToolError, WorkspaceTools


def test_read_paginates_and_numbers_lines(tmp_path):
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf8")
    result = WorkspaceTools(tmp_path, max_lines=2).read_file("notes.txt")
    assert result["content"] == "1|one\n2|two"
    assert result["truncated"] is True
    assert result["next_offset"] == 3


def test_write_requires_hash_for_overwrite(tmp_path):
    file = tmp_path / "notes.txt"
    file.write_text("old", encoding="utf8")
    tools = WorkspaceTools(tmp_path)
    with pytest.raises(ToolError):
        tools.write_file("notes.txt", "new")
    tools.write_file("notes.txt", "new", hashlib.sha256(b"old").hexdigest())
    assert file.read_text() == "new"


def test_path_cannot_escape_workspace(tmp_path):
    with pytest.raises(ToolError):
        WorkspaceTools(tmp_path).read_file("../secret.txt")


def test_hermes_replace_patch_returns_diff_and_requires_hash(tmp_path):
    file = tmp_path / "notes.txt"
    file.write_text("alpha\nbeta\n", encoding="utf8")
    tools = WorkspaceTools(tmp_path)
    sha = tools.read_file("notes.txt")["sha256"]
    result = tools.patch(mode="replace", path="notes.txt", old_string="beta", new_string="gamma", expected_sha256=sha)
    assert result["replacements"] == 1
    assert "-beta" in result["diff"] and "+gamma" in result["diff"]
    assert file.read_text() == "alpha\ngamma\n"


def test_hermes_v4a_patch_is_atomic_on_validation_failure(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\n", encoding="utf8")
    second.write_text("two\n", encoding="utf8")
    tools = WorkspaceTools(tmp_path)
    hashes = {name: tools.read_file(name)["sha256"] for name in ("first.txt", "second.txt")}
    patch = """*** Begin Patch
*** Update File: first.txt
@@
-one
+ONE
*** Update File: second.txt
@@
-missing
+TWO
*** End Patch"""
    with pytest.raises(ToolError, match="no files were modified"):
        tools.patch(mode="patch", patch=patch, expected_sha256=hashes)
    assert first.read_text() == "one\n"
    assert second.read_text() == "two\n"


def test_search_files_supports_content_and_file_targets(tmp_path):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf8")
    (tmp_path / "b.txt").write_text("nothing\n", encoding="utf8")
    tools = WorkspaceTools(tmp_path)
    content = tools.search_files("needle", file_glob="*.py")
    assert content["results"][0]["path"] == "a.py"
    files = tools.search_files("*.py", target="files")
    assert files["results"] == ["a.py"]
