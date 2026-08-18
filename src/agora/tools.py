from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


class ToolError(ValueError):
    pass


class WorkspaceTools:
    def __init__(self, root: Path, max_lines: int = 500, max_chars: int = 30_000) -> None:
        self.root = root.resolve()
        self.max_lines = max_lines
        self.max_chars = max_chars

    def resolve(self, path: str, *, allow_missing: bool = False) -> Path:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ToolError("path must be relative to the workspace")
        resolved = (self.root / candidate).resolve(strict=not allow_missing)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("path escapes the workspace") from exc
        return resolved

    def list_dir(self, path: str = ".", offset: int = 0, limit: int = 100) -> dict[str, Any]:
        directory = self.resolve(path)
        if not directory.is_dir():
            raise ToolError("path is not a directory")
        entries = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        limit = max(1, min(limit, 500))
        listed = [{"name": item.name, "path": item.relative_to(self.root).as_posix(), "type": "directory" if item.is_dir() else "file", "size": None if item.is_dir() else item.stat().st_size} for item in entries[offset:offset + limit]]
        next_offset = offset + len(listed)
        return {"path": path, "entries": listed, "offset": offset, "limit": limit, "total": len(entries), "truncated": next_offset < len(entries), "next_offset": next_offset if next_offset < len(entries) else None}

    def read_file(self, path: str, offset: int = 1, limit: int | None = None) -> dict[str, Any]:
        file = self.resolve(path)
        if not file.is_file():
            raise ToolError("path is not a file")
        raw = file.read_bytes()
        if b"\x00" in raw[:8192]:
            raise ToolError("binary files cannot be read as text")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("file is not UTF-8 text") from exc
        lines = text.splitlines()
        offset = max(1, offset)
        limit = max(1, min(limit or self.max_lines, self.max_lines))
        result: list[str] = []
        chars = 0
        for number, line in enumerate(lines[offset - 1:offset - 1 + limit], offset):
            rendered = f"{number}|{line}"
            if result and chars + len(rendered) + 1 > self.max_chars:
                break
            result.append(rendered)
            chars += len(rendered) + 1
        next_offset = offset + len(result)
        return {"path": file.relative_to(self.root).as_posix(), "content": "\n".join(result), "offset": offset, "limit": limit, "total_lines": len(lines), "truncated": next_offset <= len(lines), "next_offset": next_offset if next_offset <= len(lines) else None, "sha256": hashlib.sha256(raw).hexdigest()}

    def write_file(self, path: str, content: str, expected_sha256: str | None = None, append: bool = False) -> dict[str, Any]:
        file = self.resolve(path, allow_missing=True)
        file.parent.mkdir(parents=True, exist_ok=True)
        if file.exists() and not append:
            current = hashlib.sha256(file.read_bytes()).hexdigest()
            if expected_sha256 != current:
                raise ToolError("file changed or expected_sha256 is missing; read it before overwriting")
        mode = "ab" if append else "wb"
        if append:
            with file.open(mode) as output:
                output.write(content.encode())
        else:
            temporary = file.with_suffix(file.suffix + ".agora-tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, file)
        raw = file.read_bytes()
        return {"path": file.relative_to(self.root).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
