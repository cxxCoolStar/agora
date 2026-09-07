from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hermes_fuzzy_match import fuzzy_find_and_replace, format_no_match_hint, is_already_applied
from .hermes_patch_parser import OperationType, apply_v4a_operations, parse_v4a_patch


class ToolError(ValueError):
    pass


@dataclass
class _ReadResult:
    content: str = ""
    error: str | None = None


@dataclass
class _WriteResult:
    error: str | None = None


class WorkspaceTools:
    _SKIPPED_DIRECTORIES = frozenset({".git", ".agora", ".next", ".pytest_cache", "node_modules", "__pycache__"})

    def __init__(self, root: Path, max_lines: int = 500, max_chars: int = 30_000) -> None:
        self.root = root.resolve()
        self.max_lines = max_lines
        self.max_chars = max_chars
        self._patch_expected: dict[str, str] = {}
        self._patch_in_progress = False

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        return [
            {"type": "function", "name": "list_dir", "description": "List files and directories under a relative workspace path.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}, "offset": {"type": "integer", "minimum": 0, "default": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}}, "additionalProperties": False}},
            {"type": "function", "name": "read_file", "description": "Read a UTF-8 workspace file with stable line numbers, pagination, and sha256. Read before editing.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1, "default": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, "required": ["path"], "additionalProperties": False}},
            {"type": "function", "name": "write_file", "description": "Create or fully replace a workspace file. Existing files require expected_sha256 from read_file; use patch for targeted edits.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "expected_sha256": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}},
            {"type": "function", "name": "patch", "description": "Hermes file editor. mode='replace' targets one unique old_string; mode='patch' applies a V4A multi-file patch. Existing targets require expected_sha256.", "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["replace", "patch"], "default": "replace"}, "path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}, "patch": {"type": "string"}, "expected_sha256": {"oneOf": [{"type": "string"}, {"type": "object", "additionalProperties": {"type": "string"}}]}}, "required": ["mode"], "additionalProperties": False}},
            {"type": "function", "name": "search_files", "description": "Search UTF-8 file contents with a regex or find files by glob. Supports pagination and content/files_only/count output.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "target": {"type": "string", "enum": ["content", "files"], "default": "content"}, "path": {"type": "string", "default": "."}, "file_glob": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}, "offset": {"type": "integer", "minimum": 0, "default": 0}, "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "default": "content"}, "context": {"type": "integer", "minimum": 0, "maximum": 20, "default": 0}}, "required": ["pattern"], "additionalProperties": False}},
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        methods = {"list_dir": self.list_dir, "read_file": self.read_file, "write_file": self.write_file, "patch": self.patch, "search_files": self.search_files}
        if not isinstance(arguments, dict) or name not in methods:
            raise ToolError(f"tool is not available or arguments are invalid: {name}")
        try:
            return methods[name](**arguments)
        except TypeError as exc:
            raise ToolError(f"invalid arguments for {name}: {exc}") from exc

    def resolve(self, path: str, *, allow_missing: bool = False) -> Path:
        candidate = Path(path)
        if not isinstance(path, str) or not path.strip() or candidate.is_absolute() or ".." in candidate.parts:
            raise ToolError("path must be a non-empty relative workspace path")
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
        offset, limit = max(0, offset), max(1, min(limit, 500))
        listed = [{"name": item.name, "path": item.relative_to(self.root).as_posix(), "type": "directory" if item.is_dir() else "file", "size": None if item.is_dir() else item.stat().st_size} for item in entries[offset : offset + limit]]
        next_offset = offset + len(listed)
        return {"path": path, "entries": listed, "offset": offset, "limit": limit, "total": len(entries), "truncated": next_offset < len(entries), "next_offset": next_offset if next_offset < len(entries) else None}

    def read_file(self, path: str, offset: int = 1, limit: int | None = None) -> dict[str, Any]:
        file = self.resolve(path)
        text, raw = self._read_text(file)
        lines, offset, limit = text.splitlines(), max(1, offset), max(1, min(limit or self.max_lines, self.max_lines))
        output, chars = [], 0
        for number, line in enumerate(lines[offset - 1 : offset - 1 + limit], offset):
            rendered = f"{number}|{line}"
            if output and chars + len(rendered) + 1 > self.max_chars: break
            output.append(rendered); chars += len(rendered) + 1
        next_offset = offset + len(output)
        return {"path": file.relative_to(self.root).as_posix(), "content": "\n".join(output), "offset": offset, "limit": limit, "total_lines": len(lines), "truncated": next_offset <= len(lines), "next_offset": next_offset if next_offset <= len(lines) else None, "sha256": self._sha256(raw)}

    def patch(self, mode: str = "replace", path: str | None = None, old_string: str | None = None, new_string: str | None = None, replace_all: bool = False, patch: str | None = None, expected_sha256: str | dict[str, str] | None = None) -> dict[str, Any]:
        if mode == "replace":
            if not path or old_string is None or new_string is None: raise ToolError("replace mode requires path, old_string, and new_string")
            file = self.resolve(path); self._require_hash(file, path, expected_sha256 if isinstance(expected_sha256, str) else None)
            before, _ = self._read_text(file)
            if is_already_applied(before, old_string, new_string):
                return {"mode": "replace", "files_modified": [], "replacements": 0, "already_applied": True, "verified": True}
            after, count, strategy, error = fuzzy_find_and_replace(before, old_string, new_string, replace_all)
            if error:
                raise ToolError(error + format_no_match_hint(error, count, old_string, before))
            self._write_atomic(file, after.encode("utf-8"))
            return {"mode": "replace", "files_modified": [file.relative_to(self.root).as_posix()], "replacements": count, "match_strategy": strategy, "diff": self._diff(path, before, after), "sha256": self._sha256(after.encode()), "verified": True}
        if mode != "patch" or not patch: raise ToolError("patch mode requires non-empty patch content")
        if expected_sha256 is not None and not isinstance(expected_sha256, dict): raise ToolError("patch mode expected_sha256 must be an object mapping paths to sha256")
        operations, error = parse_v4a_patch(patch)
        if error: raise ToolError(error)
        expected = expected_sha256 or {}
        # Check every existing source before hunk matching or any write. This
        # makes a stale read a clear concurrency error, never a misleading
        # fuzzy-match failure after another file has been changed.
        for operation in operations:
            source = self.resolve(operation.file_path, allow_missing=True)
            if operation.operation is OperationType.ADD:
                if source.exists():
                    raise ToolError(f"{operation.file_path}: destination already exists")
                continue
            if source.exists():
                self._require_hash(source, operation.file_path, expected.get(operation.file_path))
        self._patch_expected = expected
        self._patch_in_progress = True
        try:
            result = apply_v4a_operations(operations, self)
        finally:
            self._patch_expected = {}
            self._patch_in_progress = False
        if getattr(result, "error", None): raise ToolError(result.error)
        return result.to_dict()

    # Native file-operation protocol consumed by the migrated Hermes patch
    # parser. Keeping it on WorkspaceTools avoids a separate compatibility
    # adapter while preserving the parser's proven two-phase algorithm.
    def read_file_raw(self, path: str) -> _ReadResult:
        try:
            file = self.resolve(path, allow_missing=True)
            text, _ = self._read_text(file)
            return _ReadResult(content=text)
        except ToolError as exc:
            return _ReadResult(error=str(exc))

    def write_file(self, path: str, content: str, expected_sha256: str | None = None, pre_content: str | None = None) -> Any:
        try:
            file = self.resolve(path, allow_missing=True)
            if file.exists():
                expected = expected_sha256 or self._patch_expected.get(path) or self._patch_expected.get(file.relative_to(self.root).as_posix())
                self._require_hash(file, path, expected)
            self._write_atomic(file, content.encode("utf-8"))
            if pre_content is not None or self._patch_in_progress:
                return _WriteResult()
            raw = file.read_bytes()
            return {"path": file.relative_to(self.root).as_posix(), "sha256": self._sha256(raw), "bytes": len(raw), "verified": True}
        except (ToolError, OSError) as exc:
            if pre_content is not None or self._patch_in_progress:
                return _WriteResult(error=str(exc))
            raise

    def delete_file(self, path: str) -> _WriteResult:
        try:
            file = self.resolve(path)
            expected = self._patch_expected.get(path) or self._patch_expected.get(file.relative_to(self.root).as_posix())
            self._require_hash(file, path, expected)
            file.unlink()
            return _WriteResult()
        except (ToolError, OSError) as exc:
            return _WriteResult(error=str(exc))

    def move_file(self, path: str, new_path: str) -> _WriteResult:
        try:
            source = self.resolve(path)
            target = self.resolve(new_path, allow_missing=True)
            expected = self._patch_expected.get(path) or self._patch_expected.get(source.relative_to(self.root).as_posix())
            self._require_hash(source, path, expected)
            if target.exists():
                raise ToolError(f"{new_path}: destination already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            return _WriteResult()
        except (ToolError, OSError) as exc:
            return _WriteResult(error=str(exc))

    def search_files(self, pattern: str, target: str = "content", path: str = ".", file_glob: str | None = None, limit: int = 50, offset: int = 0, output_mode: str = "content", context: int = 0) -> dict[str, Any]:
        if not pattern: raise ToolError("pattern must be non-empty")
        base = self.resolve(path); files = [base] if base.is_file() else list(self._iter_files(base))
        limit, offset = max(1, min(limit, 500)), max(0, offset)
        if target == "files":
            matches = sorted([f.relative_to(self.root).as_posix() for f in files if re.fullmatch(fnmatch.translate(pattern), f.name)])
            page = matches[offset : offset + limit]; nxt = offset + len(page)
            return {"target": target, "pattern": pattern, "results": page, "offset": offset, "limit": limit, "total": len(matches), "truncated": nxt < len(matches), "next_offset": nxt if nxt < len(matches) else None}
        try: expression = re.compile(pattern)
        except re.error as exc: raise ToolError(f"invalid regex: {exc}") from exc
        rows = []
        for file in files:
            if file_glob and not file.match(file_glob): continue
            try: text, _ = self._read_text(file)
            except ToolError: continue
            lines = text.splitlines(); rel = file.relative_to(self.root).as_posix(); matches = [n for n, line in enumerate(lines, 1) if expression.search(line)]
            if output_mode == "count" and matches: rows.append({"path": rel, "count": len(matches)})
            elif output_mode == "files_only" and matches: rows.append({"path": rel})
            elif output_mode == "content":
                for number in matches:
                    rows.append({"path": rel, "line": number, "content": lines[number - 1], "context": [f"{n}|{lines[n-1]}" for n in range(max(1, number-context), min(len(lines), number+context)+1)] if context else []})
        page, nxt = rows[offset : offset + limit], offset + len(rows[offset : offset + limit])
        return {"target": target, "pattern": pattern, "output_mode": output_mode, "results": page, "offset": offset, "limit": limit, "total": len(rows), "truncated": nxt < len(rows), "next_offset": nxt if nxt < len(rows) else None}

    def _iter_files(self, base: Path):
        for root, directories, names in os.walk(base, followlinks=False):
            directories[:] = [d for d in directories if d not in self._SKIPPED_DIRECTORIES]
            for name in names:
                file = Path(root) / name
                try: file.resolve().relative_to(self.root)
                except ValueError: continue
                yield file

    def _read_text(self, file: Path) -> tuple[str, bytes]:
        if not file.is_file(): raise ToolError("path is not a file")
        raw = file.read_bytes()
        if b"\x00" in raw[:8192]: raise ToolError("binary files cannot be read as text")
        try: return raw.decode("utf-8"), raw
        except UnicodeDecodeError as exc: raise ToolError("file is not UTF-8 text") from exc

    def _require_hash(self, file: Path, path: str, expected: str | None) -> None:
        if expected != self._sha256(file.read_bytes()): raise ToolError(f"{path}: file changed or expected_sha256 is missing; call read_file before modifying it")

    @staticmethod
    def _sha256(raw: bytes) -> str: return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _write_atomic(file: Path, raw: bytes) -> None:
        file.parent.mkdir(parents=True, exist_ok=True); temporary = file.with_name(f".{file.name}.agora-{uuid.uuid4().hex}.tmp")
        try: temporary.write_bytes(raw); os.replace(temporary, file)
        finally: temporary.unlink(missing_ok=True)

    @staticmethod
    def _diff(path: str, before: str, after: str) -> str:
        return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}"))
