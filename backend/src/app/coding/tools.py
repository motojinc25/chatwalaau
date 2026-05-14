"""Coding tools for the main agent (CTR-0031, PRP-0019).

Provides file read/write, shell execution, file search, and content search
as MAF function tools. All operations are restricted to the configured
workspace directory (CODING_WORKSPACE_DIR) for security.

Tools execute in a thread pool via asyncio.to_thread() to prevent blocking
the FastAPI async event loop during long-running operations.
"""

import asyncio
import json
import logging
from pathlib import Path
import re
import subprocess
from typing import Annotated

from pydantic import Field

from app.coding.security import resolve_safe_path
from app.core.config import settings

logger = logging.getLogger(__name__)

# Maximum results to prevent excessive output
_MAX_GLOB_RESULTS = 200
_MAX_GREP_RESULTS = 100


def _file_read_sync(path: str, offset: int, limit: int, max_bytes: int) -> str:
    """Synchronous file read implementation with byte-bounded safety (PRP-0047).

    Reads up to ``max_bytes`` from the file. If the file is larger, a
    ``[TRUNCATED: ...]`` marker is appended so the agent can tell the
    output was clipped and decide whether to paginate via ``offset``
    or narrow the read target.
    """
    workspace = settings.coding_workspace_dir
    try:
        safe_path = resolve_safe_path(workspace, path)
    except ValueError as e:
        return str(e)

    path_obj = Path(safe_path)
    if not path_obj.is_file():
        return f"Error: File not found: {path}"

    try:
        total_bytes = path_obj.stat().st_size
    except OSError as e:
        return f"Error reading file: {e}"

    # Normalize max_bytes: 0 or negative -> fall back to the configured cap.
    effective_max = max_bytes if max_bytes > 0 else settings.coding_file_read_max_bytes
    size_truncated = total_bytes > effective_max

    try:
        if size_truncated:
            with path_obj.open("rb") as fb:
                raw = fb.read(effective_max)
            text = raw.decode("utf-8", errors="replace")
        else:
            with path_obj.open(encoding="utf-8", errors="replace") as f:
                text = f.read()
    except PermissionError:
        return f"Error: Permission denied: {path}"
    except OSError as e:
        return f"Error reading file: {e}"

    # splitlines without keepends so the numbered render is clean.
    lines = text.splitlines()
    total = len(lines)

    if offset < 0:
        offset = 0
    if offset >= total:
        return f"Error: offset {offset} exceeds file length ({total} lines" + (
            f", file truncated at {effective_max} bytes of {total_bytes})" if size_truncated else ")"
        )

    effective_limit = limit if limit > 0 else total
    selected = lines[offset : offset + effective_limit]
    line_truncated = offset + len(selected) < total

    numbered = [f"{i:>6}\t{line}" for i, line in enumerate(selected, start=offset + 1)]

    header_parts = [
        f"File: {path}",
        f"lines {offset + 1}-{offset + len(selected)} of {total}",
    ]
    if size_truncated:
        header_parts.append(f"file truncated at {effective_max} of {total_bytes} bytes")
    header = " | ".join(header_parts)

    footer_parts: list[str] = []
    if size_truncated:
        footer_parts.append(
            f"[TRUNCATED BY BYTES: read {effective_max} of {total_bytes} bytes; "
            "re-invoke with a narrower path or a larger CODING_FILE_READ_MAX_BYTES]"
        )
    if line_truncated:
        footer_parts.append(
            f"[TRUNCATED BY LIMIT: shown {len(selected)} of {total} lines; "
            f"continue with offset={offset + len(selected)}]"
        )

    body = "\n".join(numbered)
    if footer_parts:
        body = body + "\n" + "\n".join(footer_parts)
    return header + "\n" + body


def _file_write_sync(path: str, content: str) -> str:
    """Synchronous file write implementation."""
    workspace = settings.coding_workspace_dir
    try:
        safe_path = resolve_safe_path(workspace, path)
    except ValueError as e:
        return str(e)

    try:
        Path(safe_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(safe_path).open("w", encoding="utf-8") as f:
            written = f.write(content)
        logger.info("Written %d chars to %s", written, path)
        return f"Successfully written {written} characters to {path}"
    except PermissionError:
        return f"Error: Permission denied: {path}"
    except OSError as e:
        return f"Error writing file: {e}"


def _bash_execute_sync(command: str, cwd: str) -> str:
    """Synchronous bash execute implementation."""
    workspace = settings.coding_workspace_dir
    timeout = settings.coding_bash_timeout
    max_output = settings.coding_max_output_chars

    if cwd:
        try:
            work_dir = resolve_safe_path(workspace, cwd)
        except ValueError as e:
            return str(e)
    else:
        work_dir = workspace

    if not Path(work_dir).is_dir():
        return f"Error: Working directory not found: {cwd or workspace}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=work_dir,
        )
        output = result.stdout + result.stderr
        if len(output) > max_output:
            output = output[:max_output] + f"\n... (output truncated at {max_output} characters)"
        return f"Exit code: {result.returncode}\n{output}"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout} seconds: {command}"
    except OSError as e:
        return f"Error executing command: {e}"


def _file_glob_sync(pattern: str, path: str) -> str:
    """Synchronous file glob implementation."""
    workspace = settings.coding_workspace_dir

    if path:
        try:
            base = resolve_safe_path(workspace, path)
        except ValueError as e:
            return str(e)
    else:
        base = workspace

    if not Path(base).is_dir():
        return f"Error: Directory not found: {path or workspace}"

    base_path = Path(base)
    matches = []
    workspace_path = Path(workspace)
    for match in base_path.glob(pattern):
        if match.is_file():
            try:
                rel = match.relative_to(workspace_path)
                matches.append(str(rel).replace("\\", "/"))
            except ValueError:
                continue
        if len(matches) >= _MAX_GLOB_RESULTS:
            break

    return json.dumps(matches)


def _file_grep_sync(pattern: str, path: str, include: str) -> str:
    """Synchronous file grep implementation."""
    workspace = settings.coding_workspace_dir

    if path:
        try:
            base = resolve_safe_path(workspace, path)
        except ValueError as e:
            return str(e)
    else:
        base = workspace

    if not Path(base).is_dir():
        return f"Error: Directory not found: {path or workspace}"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: Invalid regex pattern: {e}"

    results = []
    workspace_path = Path(workspace)
    base_path = Path(base)
    glob_pattern = include or "**/*"

    for file_path in base_path.glob(glob_pattern):
        if not file_path.is_file():
            continue
        try:
            with file_path.open(encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, start=1):
                    if regex.search(line):
                        try:
                            rel = file_path.relative_to(workspace_path)
                            rel_str = str(rel).replace("\\", "/")
                        except ValueError:
                            continue
                        results.append(f"{rel_str}:{line_num}:{line.rstrip()}")
                        if len(results) >= _MAX_GREP_RESULTS:
                            break
        except (PermissionError, OSError):
            continue
        if len(results) >= _MAX_GREP_RESULTS:
            break

    if not results:
        return f"No matches found for pattern: {pattern}"
    return "\n".join(results)


# ---- Public async tool functions (registered on MAF agent) ----


async def file_read(
    path: Annotated[str, Field(description="Relative file path from the workspace directory")],
    offset: Annotated[int, Field(description="Line number to start reading from (0-based, default 0)")] = 0,
    limit: Annotated[int, Field(description="Maximum number of lines to read (default 2000)")] = 2000,
    max_bytes: Annotated[
        int,
        Field(
            description=(
                "Maximum bytes to read from the file (default from CODING_FILE_READ_MAX_BYTES, "
                "typically 1048576). Files larger than this are truncated; the response includes "
                "a [TRUNCATED BY BYTES: ...] marker."
            ),
        ),
    ] = 0,
) -> str:
    """Read file content with line numbers. Use offset/limit for large files.

    Output includes explicit ``[TRUNCATED BY BYTES: ...]`` or
    ``[TRUNCATED BY LIMIT: ...]`` markers when the file exceeds the byte
    cap or the line limit (PRP-0047). Pass ``max_bytes=0`` to accept
    the configured default; pass a positive value to tighten the read
    for a specific call.
    """
    return await asyncio.to_thread(_file_read_sync, path, offset, limit, max_bytes)


async def file_write(
    path: Annotated[str, Field(description="Relative file path from the workspace directory")],
    content: Annotated[str, Field(description="Full file content to write")],
) -> str:
    """Write content to a file. Creates parent directories if needed."""
    return await asyncio.to_thread(_file_write_sync, path, content)


async def bash_execute(
    command: Annotated[str, Field(description="Shell command to execute")],
    cwd: Annotated[str, Field(description="Working directory relative to workspace (default: workspace root)")] = "",
) -> str:
    """Execute a shell command in the workspace directory. Returns stdout, stderr, and exit code."""
    return await asyncio.to_thread(_bash_execute_sync, command, cwd)


async def file_glob(
    pattern: Annotated[str, Field(description="Glob pattern to match files (e.g., '**/*.py', 'src/**/*.ts')")],
    path: Annotated[str, Field(description="Base directory relative to workspace (default: workspace root)")] = "",
) -> str:
    """Find files matching a glob pattern in the workspace directory."""
    return await asyncio.to_thread(_file_glob_sync, pattern, path)


async def file_grep(
    pattern: Annotated[str, Field(description="Regex pattern to search for in file contents")],
    path: Annotated[
        str, Field(description="Directory relative to workspace to search in (default: workspace root)")
    ] = "",
    include: Annotated[str, Field(description="Glob filter for file types (e.g., '**/*.py', '**/*.ts')")] = "",
) -> str:
    """Search file contents by regex pattern in the workspace directory."""
    return await asyncio.to_thread(_file_grep_sync, pattern, path, include)
