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
