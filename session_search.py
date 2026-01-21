#!/usr/bin/env python3
"""
Search Codex/Gemini/OpenCode(CLI)/Cursor native session stores and export a selected
session as a normalized JSON file (similar to extract_*.py outputs).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class Match:
    source: str
    session_id: Optional[str]
    sort_ts: Optional[float]
    display_ts: str
    snippet: str
    meta: str
    export_ref: dict[str, Any] = field(default_factory=dict)


def _iter_occurrences(text: str, needle: str) -> Iterable[int]:
    if not needle:
        return
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            return
        yield idx
        start = idx + 1


def _compact_one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _tildeify(text: str) -> str:
    if not text:
        return text
    home = str(Path.home())
    if not home:
        return text
    return text.replace(home, "~")


def _use_ansi_color() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    term = (os.environ.get("TERM") or "").lower()
    if term == "dumb":
        return False
    return True


def _highlight(text: str, needle: str) -> str:
    if not needle or needle not in text:
        return text
    if not _use_ansi_color():
        return text.replace(needle, f"⟦{needle}⟧")
    start = "\x1b[1;37m"  # bold white
    end = "\x1b[0m"
    return text.replace(needle, f"{start}{needle}{end}")


def _make_snippet(text: str, idx: int, needle_len: int, context_chars: int) -> str:
    start = max(0, idx - context_chars)
    end = min(len(text), idx + needle_len + context_chars)
    return _compact_one_line(text[start:end])


def _safe_filename(text: str, max_len: int = 180) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    if not cleaned:
        cleaned = "export"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def _safe_piece(text: str, max_len: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def _unique_path(output_dir: Path, filename: str) -> Path:
    path = output_dir / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(1, 10_000):
        candidate = output_dir / f"{stem}__{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique filename for {filename}")


def _to_sort_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # ms since epoch
            return v / 1000.0
        return v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt.timestamp()
    return None


def _format_ts(value: Any) -> str:
    if value is None:
        return "?"
    if isinstance(value, (int, float)):
        secs = float(value)
        if secs > 1e12:
            secs = secs / 1000.0
        try:
            return datetime.fromtimestamp(secs).isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError):
            return str(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "?"
        had_z = s.endswith("Z")
        s2 = s[:-1] + "+00:00" if had_z else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            return s

        dt = dt.replace(microsecond=0)
        if dt.tzinfo is not None:
            offset = dt.utcoffset()
            if offset == timedelta(0):
                dt_utc = dt.astimezone(timezone.utc)
                return dt_utc.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
            return dt.isoformat(timespec="seconds")
        return dt.isoformat(timespec="seconds")
    return str(value)


def _rg_files_with_matches(query: str, paths: list[Path], globs: Optional[list[str]] = None) -> list[Path]:
    if not shutil.which("rg"):
        return []
    cmd: list[str] = [
        "rg",
        "-F",
        "--files-with-matches",
        "--no-messages",
        "--hidden",
        "--no-ignore",
    ]
    if globs:
        for g in globs:
            cmd.extend(["--glob", g])
    cmd.append("--")
    cmd.append(query)
    cmd.extend([str(p) for p in paths])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception:
        return []

    if proc.returncode not in (0, 1):
        return []

    files: list[Path] = []
    for line in proc.stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        files.append(Path(s))
    return files


def _write_json_export(output_dir: Path, base_name: str, payload: dict[str, Any]) -> Path:
    out_path = _unique_path(output_dir, f"{_safe_filename(base_name)}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return out_path


def _pick_codex_text_for_snippet(obj: Optional[dict[str, Any]], fallback_line: str, query: str) -> str:
    if not obj:
        return fallback_line
    if query in fallback_line:
        raw_fallback = fallback_line
    else:
        raw_fallback = json.dumps(obj, ensure_ascii=False)

    event_type = obj.get("type")
    if event_type != "event_msg":
        return raw_fallback

    payload = obj.get("payload", {}) or {}
    payload_type = payload.get("type")

    candidates: list[Any] = []
    if payload_type in ("user_message", "agent_message"):
        candidates.append(payload.get("message"))
    candidates.append(payload.get("output"))
    candidates.append(payload.get("diff"))
    candidates.append(payload.get("context"))
    candidates.append(payload.get("input"))

    for c in candidates:
        if c is None:
            continue
        if isinstance(c, str):
            text = c
        else:
            try:
                text = json.dumps(c, ensure_ascii=False)
            except Exception:
                continue
        if query in text:
            return text

    return raw_fallback


def search_codex(query: str, context_chars: int, use_rg: bool) -> list[Match]:
    try:
        import extract_codex
    except Exception:
        return []

    matches: list[Match] = []
    installations = extract_codex.find_codex_installations()
    for installation in installations:
        search_roots: list[Path] = []
        sessions_dir = installation / "sessions"
        projects_dir = installation / "projects"
        if sessions_dir.exists():
            search_roots.append(sessions_dir)
        if projects_dir.exists():
            search_roots.append(projects_dir)
        if not search_roots:
            continue

        if use_rg:
            candidate_files = _rg_files_with_matches(query, search_roots, globs=["*.jsonl"])
        else:
            candidate_files = extract_codex.find_all_codex_sessions(installation)

        for session_file in candidate_files:
            session_meta: dict[str, Any] = {}
            file_matches: list[Match] = []
            try:
                with open(session_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, start=1):
                        raw_line = line.rstrip("\n")

                        # Capture session metadata even if the search term isn't in the meta line.
                        if not session_meta and "session_meta" in raw_line:
                            try:
                                meta_obj = json.loads(raw_line)
                                if isinstance(meta_obj, dict) and meta_obj.get("type") == "session_meta":
                                    session_meta = meta_obj.get("payload", {}) or {}
                            except json.JSONDecodeError:
                                pass

                        if query not in raw_line:
                            continue

                        obj: Optional[dict[str, Any]] = None
                        try:
                            obj = json.loads(raw_line)
                        except json.JSONDecodeError:
                            obj = None

                        ts_value = obj.get("timestamp") if obj else None
                        text_for_snippet = _pick_codex_text_for_snippet(obj, raw_line, query)

                        for idx in _iter_occurrences(text_for_snippet, query):
                            snippet = _make_snippet(text_for_snippet, idx, len(query), context_chars)
                            session_id = session_meta.get("id")
                            cwd = session_meta.get("cwd")
                            meta_parts: list[str] = []
                            if cwd:
                                meta_parts.append(f"cwd={cwd}")
                            meta = " ".join(meta_parts)

                            file_matches.append(
                                Match(
                                    source="codex",
                                    session_id=session_id,
                                    sort_ts=_to_sort_ts(ts_value) or _to_sort_ts(session_meta.get("timestamp")),
                                    display_ts=_format_ts(ts_value) if ts_value is not None else _format_ts(session_meta.get("timestamp")),
                                    snippet=snippet,
                                    meta=_compact_one_line(meta),
                                    export_ref={"source": "codex", "session_file": str(session_file)},
                                )
                            )
            except (OSError, UnicodeError):
                continue

            if session_meta:
                updated: list[Match] = []
                for m in file_matches:
                    if m.session_id:
                        updated.append(m)
                        continue
                    updated.append(
                        Match(
                            source=m.source,
                            session_id=session_meta.get("id"),
                            sort_ts=m.sort_ts,
                            display_ts=m.display_ts,
                            snippet=m.snippet,
                            meta=m.meta,
                            export_ref=m.export_ref,
                        )
                    )
                matches.extend(updated)
            else:
                matches.extend(file_matches)

    return matches


def search_gemini(query: str, context_chars: int, use_rg: bool) -> list[Match]:
    try:
        import extract_gemini
    except Exception:
        return []

    matches: list[Match] = []
    installations = extract_gemini.find_gemini_installations()
    for installation in installations:
        tmp_dir = installation / "tmp"
        if not tmp_dir.exists():
            continue

        if use_rg:
            candidate_files = _rg_files_with_matches(query, [tmp_dir], globs=["session-*.json"])
        else:
            candidate_files = extract_gemini.find_all_gemini_sessions(installation)

        for session_file in candidate_files:
            try:
                with open(session_file, "r", encoding="utf-8", errors="ignore") as f:
                    data = json.load(f)
            except Exception:
                continue

            session_id = data.get("sessionId")
            project_hash = data.get("projectHash")
            start_time = data.get("startTime")
            last_updated = data.get("lastUpdated")

            for msg_idx, msg in enumerate(data.get("messages", []) or []):
                if not isinstance(msg, dict):
                    continue

                msg_type = msg.get("type")
                content = msg.get("content", "")
                thoughts = msg.get("thoughts")
                ts_value = msg.get("timestamp") or last_updated or start_time

                candidates: list[str] = []
                if isinstance(content, str) and content:
                    candidates.append(content)
                if thoughts:
                    try:
                        candidates.append(json.dumps(thoughts, ensure_ascii=False))
                    except Exception:
                        pass
                try:
                    candidates.append(json.dumps(msg, ensure_ascii=False))
                except Exception:
                    pass

                for text in candidates:
                    if not text or query not in text:
                        continue
                    for idx in _iter_occurrences(text, query):
                        snippet = _make_snippet(text, idx, len(query), context_chars)
                        meta_parts: list[str] = []
                        if project_hash:
                            meta_parts.append(f"project={project_hash}")
                        if msg_type:
                            meta_parts.append(f"type={msg_type}")
                        meta_parts.append(f"msg={msg_idx}")
                        meta = " ".join(meta_parts)

                        matches.append(
                            Match(
                                source="gemini-cli",
                                session_id=session_id,
                                sort_ts=_to_sort_ts(ts_value),
                                display_ts=_format_ts(ts_value),
                                snippet=snippet,
                                meta=_compact_one_line(meta),
                                export_ref={"source": "gemini-cli", "session_file": str(session_file)},
                            )
                        )
                    break

    return matches


def _walk_files(root: Path, suffix: str = ".json") -> Iterable[Path]:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if suffix and not name.endswith(suffix):
                continue
            yield Path(dirpath) / name


def _load_json_maybe(path: Path) -> Optional[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        return None


def search_opencode_cli(query: str, context_chars: int, use_rg: bool) -> list[Match]:
    try:
        import extract_opencode
    except Exception:
        return []

    matches: list[Match] = []
    installations = [p for t, p in extract_opencode.find_opencode_installations() if t == "cli"]

    for storage_base in installations:
        part_root = storage_base / "part"
        if not part_root.exists():
            continue

        if use_rg:
            part_files = _rg_files_with_matches(query, [part_root], globs=["*.json"])
        else:
            part_files = []
            for path in _walk_files(part_root, suffix=".json"):
                try:
                    if query in path.read_text(encoding="utf-8", errors="ignore"):
                        part_files.append(path)
                except Exception:
                    continue

        if not part_files:
            continue

        needed_message_ids = {p.parent.name for p in part_files}

        message_root = storage_base / "message"
        message_to_session: dict[str, str] = {}
        message_meta_cache: dict[str, dict[str, Any]] = {}
        if message_root.exists():
            for session_dir in sorted(message_root.iterdir(), key=lambda p: p.name):
                if not session_dir.is_dir():
                    continue
                for message_file in session_dir.iterdir():
                    if message_file.suffix != ".json":
                        continue
                    msg_id = message_file.stem
                    if msg_id not in needed_message_ids:
                        continue
                    message_to_session[msg_id] = session_dir.name
                    meta = _load_json_maybe(message_file)
                    if meta:
                        message_meta_cache[msg_id] = meta

        session_root = storage_base / "session"
        session_file_by_id: dict[str, Path] = {}
        if session_root.exists():
            for project_dir in sorted(session_root.iterdir(), key=lambda p: p.name):
                if not project_dir.is_dir():
                    continue
                for sess_file in project_dir.iterdir():
                    if sess_file.suffix != ".json":
                        continue
                    session_file_by_id[sess_file.stem] = sess_file

        session_meta_cache: dict[str, dict[str, Any]] = {}
        project_meta_cache: dict[str, dict[str, Any]] = {}

        def get_session_meta(session_id: str) -> dict[str, Any]:
            if session_id in session_meta_cache:
                return session_meta_cache[session_id]
            sess_file = session_file_by_id.get(session_id)
            data = _load_json_maybe(sess_file) if sess_file else None
            session_meta_cache[session_id] = data or {}
            return session_meta_cache[session_id]

        def get_project_meta(project_id: str) -> dict[str, Any]:
            if project_id in project_meta_cache:
                return project_meta_cache[project_id]
            proj_file = storage_base / "project" / f"{project_id}.json"
            data = _load_json_maybe(proj_file) or {}
            project_meta_cache[project_id] = data
            return data

        for part_file in part_files:
            part_data = _load_json_maybe(part_file)
            if not part_data:
                continue

            message_id = part_file.parent.name
            session_id = message_to_session.get(message_id)

            text = part_data.get("text")
            if isinstance(text, str) and text:
                haystack = text
            else:
                try:
                    haystack = json.dumps(part_data, ensure_ascii=False)
                except Exception:
                    continue

            if query not in haystack:
                continue

            part_type = part_data.get("type")
            part_time = (part_data.get("time") or {}).get("created")

            msg_meta = message_meta_cache.get(message_id, {})
            msg_time = (msg_meta.get("time") or {}).get("created") if isinstance(msg_meta, dict) else None

            sort_ts = _to_sort_ts(part_time) or _to_sort_ts(msg_time)
            display_ts = _format_ts(part_time) if part_time is not None else _format_ts(msg_time)

            cwd = None
            title = None
            project_id = None
            last_updated = None
            if session_id:
                sess_meta = get_session_meta(session_id)
                if sess_meta:
                    project_id = sess_meta.get("projectID")
                    title = sess_meta.get("title")
                    last_updated = (sess_meta.get("time") or {}).get("updated")
                if project_id:
                    proj_meta = get_project_meta(project_id)
                    cwd = proj_meta.get("path") or proj_meta.get("cwd")
                if sort_ts is None:
                    sort_ts = _to_sort_ts(last_updated)
                    display_ts = _format_ts(last_updated)

            for idx in _iter_occurrences(haystack, query):
                snippet = _make_snippet(haystack, idx, len(query), context_chars)
                meta_parts: list[str] = []
                if cwd:
                    meta_parts.append(f"cwd={cwd}")
                if title:
                    meta_parts.append(f"title={title}")
                if part_type:
                    meta_parts.append(f"part={part_type}")
                if session_id:
                    meta_parts.append(f"session={session_id}")
                meta = " ".join(meta_parts)

                matches.append(
                    Match(
                        source="opencode",
                        session_id=session_id,
                        sort_ts=sort_ts,
                        display_ts=display_ts,
                        snippet=snippet,
                        meta=_compact_one_line(meta),
                        export_ref={
                            "source": "opencode",
                            "storage_base": str(storage_base),
                            "session_id": session_id,
                        },
                    )
                )

    return matches


def _connect_sqlite_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _cursor_fetchone_json(cursor: sqlite3.Cursor, sql: str, params: tuple[Any, ...]) -> Optional[Any]:
    cursor.execute(sql, params)
    row = cursor.fetchone()
    if not row or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _cursor_export_chat(db_path: Path, workspace_id: str, tab_id: str) -> Optional[dict[str, Any]]:
    try:
        conn = _connect_sqlite_ro(db_path)
        cur = conn.cursor()
        data = _cursor_fetchone_json(
            cur,
            "SELECT value FROM ItemTable WHERE key = ?",
            ("workbench.panel.aichat.view.aichat.chatdata",),
        )
        conn.close()
    except Exception:
        return None

    if not isinstance(data, dict) or "tabs" not in data:
        return None

    for tab in data.get("tabs", []) or []:
        if not isinstance(tab, dict):
            continue
        if tab.get("tabId") != tab_id:
            continue

        messages: list[dict[str, Any]] = []
        bubbles = tab.get("bubbles", []) or []
        for bubble in bubbles:
            if not isinstance(bubble, dict):
                continue
            bubble_type = bubble.get("type")
            content = bubble.get("rawText", bubble.get("text", "")) or ""
            msg = {
                "role": "user" if bubble_type == "user" else "assistant",
                "content": content,
            }

            if bubble.get("selections"):
                ctx = []
                for sel in bubble.get("selections", []) or []:
                    if not isinstance(sel, dict):
                        continue
                    uri = sel.get("uri") or {}
                    if isinstance(uri, dict) and "fsPath" in uri:
                        ctx.append(
                            {
                                "file": uri["fsPath"],
                                "code": sel.get("text", sel.get("rawText", "")),
                                "range": sel.get("range"),
                            }
                        )
                if ctx:
                    msg["code_context"] = ctx

            if bubble.get("suggestedDiffs"):
                msg["suggested_diffs"] = bubble.get("suggestedDiffs")

            messages.append(msg)

        return {
            "messages": messages,
            "source": "cursor-chat",
            "chat_title": tab.get("chatTitle"),
            "tab_id": tab_id,
            "workspace_id": workspace_id,
            "db_path": str(db_path),
        }

    return None


def _cursor_export_workspace_composer(db_path: Path, workspace_id: str, composer_id: str) -> Optional[dict[str, Any]]:
    try:
        conn = _connect_sqlite_ro(db_path)
        cur = conn.cursor()
        data = _cursor_fetchone_json(
            cur,
            "SELECT value FROM ItemTable WHERE key = ?",
            ("composer.composerData",),
        )
        conn.close()
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    all_composers = data.get("allComposers", []) or []
    for composer_data in all_composers:
        if not isinstance(composer_data, dict):
            continue
        if composer_data.get("composerId") != composer_id:
            continue

        messages: list[dict[str, Any]] = []
        conversation = composer_data.get("conversation", []) or []
        for bubble in conversation:
            if not isinstance(bubble, dict):
                continue
            bubble_type = bubble.get("type")
            text = bubble.get("text", "") or ""
            if bubble_type == 1:
                msg: dict[str, Any] = {"role": "user", "content": text}
                context = bubble.get("context") or {}
                if isinstance(context, dict) and context.get("selections"):
                    ctx = []
                    for sel in context.get("selections", []) or []:
                        if not isinstance(sel, dict):
                            continue
                        uri = sel.get("uri") or {}
                        if isinstance(uri, dict) and "fsPath" in uri:
                            ctx.append(
                                {
                                    "file": uri["fsPath"],
                                    "code": sel.get("text", sel.get("rawText", "")),
                                    "range": sel.get("range"),
                                }
                            )
                    if ctx:
                        msg["code_context"] = ctx
                messages.append(msg)
            elif bubble_type == 2:
                msg = {"role": "assistant", "content": text}
                if bubble.get("codeBlocks"):
                    msg["code_blocks"] = bubble.get("codeBlocks")
                if bubble.get("suggestedCodeBlocks"):
                    msg["suggested_code_blocks"] = bubble.get("suggestedCodeBlocks")
                if bubble.get("diffHistories"):
                    msg["diff_histories"] = bubble.get("diffHistories")
                messages.append(msg)

        return {
            "messages": messages,
            "source": "cursor-workspace-composer",
            "composer_id": composer_id,
            "name": composer_data.get("name", "Untitled"),
            "workspace_id": workspace_id,
            "created_at": composer_data.get("createdAt"),
            "updated_at": composer_data.get("lastUpdatedAt"),
            "db_path": str(db_path),
        }

    return None


def _cursor_export_aiservice(db_path: Path, workspace_id: str, index: int) -> Optional[dict[str, Any]]:
    try:
        conn = _connect_sqlite_ro(db_path)
        cur = conn.cursor()
        prompts = _cursor_fetchone_json(cur, "SELECT value FROM ItemTable WHERE key = ?", ("aiService.prompts",)) or []
        generations = _cursor_fetchone_json(cur, "SELECT value FROM ItemTable WHERE key = ?", ("aiService.generations",)) or []
        conn.close()
    except Exception:
        return None

    if not isinstance(prompts, list) or not isinstance(generations, list):
        return None

    if index < 0 or index >= max(len(prompts), len(generations)):
        return None

    messages: list[dict[str, Any]] = []
    if index < len(prompts) and isinstance(prompts[index], dict):
        messages.append(
            {
                "role": "user",
                "content": prompts[index].get("text", ""),
                "command_type": prompts[index].get("commandType"),
            }
        )
    if index < len(generations) and isinstance(generations[index], dict):
        messages.append(
            {
                "role": "assistant",
                "content": generations[index].get("text", generations[index].get("message", "")),
            }
        )

    if not messages:
        return None

    return {
        "messages": messages,
        "source": "cursor-aiservice",
        "workspace_id": workspace_id,
        "index": index,
        "db_path": str(db_path),
    }


def _cursor_export_global_composer(global_db_path: Path, composer_id: str) -> Optional[dict[str, Any]]:
    try:
        conn = _connect_sqlite_ro(global_db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{composer_id}",),
        )
        row = cur.fetchone()
        composer_json = row[0] if row and row[0] else None
        composer_data = json.loads(composer_json) if composer_json else None
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None

    if not isinstance(composer_data, dict):
        try:
            conn.close()
        except Exception:
            pass
        return None

    name = composer_data.get("name", "Untitled")
    created_at = composer_data.get("createdAt")
    updated_at = composer_data.get("lastUpdatedAt")
    status = composer_data.get("status")
    unified_mode = composer_data.get("unifiedMode")

    messages: list[dict[str, Any]] = []

    inline_conversation = composer_data.get("conversation", []) or []
    storage_type = "inline" if inline_conversation else "separate"

    if inline_conversation:
        for bubble in inline_conversation:
            if not isinstance(bubble, dict):
                continue
            bubble_type = bubble.get("type")
            text = bubble.get("text", "") or ""
            if bubble_type == 1:
                msg: dict[str, Any] = {"role": "user", "content": text}
                context = bubble.get("context") or {}
                if isinstance(context, dict) and context.get("selections"):
                    ctx = []
                    for sel in context.get("selections", []) or []:
                        if not isinstance(sel, dict):
                            continue
                        uri = sel.get("uri") or {}
                        if isinstance(uri, dict) and "fsPath" in uri:
                            ctx.append(
                                {
                                    "file": uri["fsPath"],
                                    "code": sel.get("text", sel.get("rawText", "")),
                                    "range": sel.get("range"),
                                }
                            )
                    if ctx:
                        msg["code_context"] = ctx
                messages.append(msg)
            elif bubble_type == 2:
                msg = {"role": "assistant", "content": text}
                if bubble.get("codeBlocks"):
                    msg["code_blocks"] = bubble.get("codeBlocks")
                if bubble.get("suggestedCodeBlocks"):
                    msg["suggested_code_blocks"] = bubble.get("suggestedCodeBlocks")
                if bubble.get("diffHistories"):
                    msg["diff_histories"] = bubble.get("diffHistories")
                if bubble.get("toolResults"):
                    msg["tool_results"] = bubble.get("toolResults")
                messages.append(msg)
    else:
        try:
            cur.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE ? AND value IS NOT NULL",
                (f"bubbleId:{composer_id}:%",),
            )
            rows = cur.fetchall()
        except Exception:
            rows = []

        parsed: list[tuple[Any, dict[str, Any]]] = []
        for key, value in rows:
            try:
                bubble_data = json.loads(value) if value else None
            except Exception:
                bubble_data = None
            if not isinstance(bubble_data, dict):
                continue
            bubble_created = bubble_data.get("createdAt") or bubble_data.get("timestamp")
            parsed.append((bubble_created, bubble_data))

        parsed.sort(key=lambda t: (_to_sort_ts(t[0]) or 0.0))

        for _created, bubble_data in parsed:
            bubble_type = bubble_data.get("type")
            text = bubble_data.get("text", "") or ""
            msg: dict[str, Any] = {
                "role": "user" if bubble_type == 1 else "assistant",
                "content": text,
            }
            messages.append(msg)

    try:
        conn.close()
    except Exception:
        pass

    if not messages:
        return None

    return {
        "messages": messages,
        "source": "cursor-global-composer",
        "composer_id": composer_id,
        "name": name,
        "status": status,
        "unified_mode": unified_mode,
        "created_at": created_at,
        "updated_at": updated_at,
        "storage_type": storage_type,
        "db_path": str(global_db_path),
    }


def search_cursor(query: str, context_chars: int) -> list[Match]:
    try:
        import extract_cursor
    except Exception:
        return []

    matches: list[Match] = []
    installations = extract_cursor.find_cursor_installations()
    for installation in installations:
        workspace_storage = installation / "User" / "workspaceStorage"
        if workspace_storage.exists():
            for workspace_dir in workspace_storage.iterdir():
                if not workspace_dir.is_dir() or workspace_dir.name == "ext-dev":
                    continue
                workspace_id = workspace_dir.name
                db_path = workspace_dir / "state.vscdb"
                if not db_path.exists():
                    continue
                db_mtime = db_path.stat().st_mtime

                # Chat mode
                try:
                    conn = _connect_sqlite_ro(db_path)
                    cur = conn.cursor()
                    chat_data = _cursor_fetchone_json(
                        cur,
                        "SELECT value FROM ItemTable WHERE key = ?",
                        ("workbench.panel.aichat.view.aichat.chatdata",),
                    )
                    composer_data = _cursor_fetchone_json(
                        cur,
                        "SELECT value FROM ItemTable WHERE key = ?",
                        ("composer.composerData",),
                    )
                    prompts = _cursor_fetchone_json(cur, "SELECT value FROM ItemTable WHERE key = ?", ("aiService.prompts",)) or []
                    generations = _cursor_fetchone_json(cur, "SELECT value FROM ItemTable WHERE key = ?", ("aiService.generations",)) or []
                    conn.close()
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    chat_data = None
                    composer_data = None
                    prompts = []
                    generations = []

                if isinstance(chat_data, dict) and isinstance(chat_data.get("tabs"), list):
                    for tab in chat_data.get("tabs", []) or []:
                        if not isinstance(tab, dict):
                            continue
                        tab_id = tab.get("tabId")
                        if not tab_id:
                            continue
                        chat_title = tab.get("chatTitle")
                        for bubble_idx, bubble in enumerate(tab.get("bubbles", []) or []):
                            if not isinstance(bubble, dict):
                                continue
                            content = bubble.get("rawText", bubble.get("text", "")) or ""
                            haystack = content
                            if query not in haystack:
                                try:
                                    haystack = json.dumps(bubble, ensure_ascii=False)
                                except Exception:
                                    haystack = content
                            if query not in haystack:
                                continue

                            for idx in _iter_occurrences(haystack, query):
                                snippet = _make_snippet(haystack, idx, len(query), context_chars)
                                meta_parts = [
                                    f"ws={workspace_id}",
                                    f"tab={tab_id}",
                                ]
                                if chat_title:
                                    meta_parts.append(f"title={chat_title}")
                                meta_parts.append(f"bubble={bubble_idx}")
                                meta_parts.append(f"db={db_path}")
                                meta = " ".join(meta_parts)

                                matches.append(
                                    Match(
                                        source="cursor-chat",
                                        session_id=str(tab_id),
                                        sort_ts=db_mtime,
                                        display_ts=_format_ts(db_mtime),
                                        snippet=snippet,
                                        meta=_compact_one_line(meta),
                                        export_ref={
                                            "source": "cursor-chat",
                                            "db_path": str(db_path),
                                            "workspace_id": workspace_id,
                                            "tab_id": tab_id,
                                        },
                                    )
                                )

                if isinstance(composer_data, dict) and isinstance(composer_data.get("allComposers"), list):
                    for composer in composer_data.get("allComposers", []) or []:
                        if not isinstance(composer, dict):
                            continue
                        composer_id = composer.get("composerId")
                        if not composer_id:
                            continue
                        name = composer.get("name", "Untitled")
                        created_at = composer.get("createdAt")
                        updated_at = composer.get("lastUpdatedAt")
                        ts_value = updated_at or created_at or db_mtime
                        conversation = composer.get("conversation", []) or []
                        for bubble_idx, bubble in enumerate(conversation):
                            if not isinstance(bubble, dict):
                                continue
                            text = bubble.get("text", "") or ""
                            haystack = text
                            if query not in haystack:
                                try:
                                    haystack = json.dumps(bubble, ensure_ascii=False)
                                except Exception:
                                    haystack = text
                            if query not in haystack:
                                continue

                            for idx in _iter_occurrences(haystack, query):
                                snippet = _make_snippet(haystack, idx, len(query), context_chars)
                                meta = _compact_one_line(
                                    f"ws={workspace_id} composer={composer_id} name={name} bubble={bubble_idx} db={db_path}"
                                )
                                matches.append(
                                    Match(
                                        source="cursor-workspace-composer",
                                        session_id=str(composer_id) if composer_id else None,
                                        sort_ts=_to_sort_ts(ts_value),
                                        display_ts=_format_ts(ts_value),
                                        snippet=snippet,
                                        meta=meta,
                                        export_ref={
                                            "source": "cursor-workspace-composer",
                                            "db_path": str(db_path),
                                            "workspace_id": workspace_id,
                                            "composer_id": composer_id,
                                        },
                                    )
                                )

                if isinstance(prompts, list) or isinstance(generations, list):
                    max_len = max(len(prompts) if isinstance(prompts, list) else 0, len(generations) if isinstance(generations, list) else 0)
                    for i in range(max_len):
                        texts: list[str] = []
                        if i < len(prompts) and isinstance(prompts[i], dict):
                            texts.append(prompts[i].get("text", "") or "")
                        if i < len(generations) and isinstance(generations[i], dict):
                            texts.append(generations[i].get("text", generations[i].get("message", "")) or "")
                        for t in texts:
                            if not t or query not in t:
                                continue
                            for idx in _iter_occurrences(t, query):
                                snippet = _make_snippet(t, idx, len(query), context_chars)
                                meta = _compact_one_line(f"ws={workspace_id} idx={i} db={db_path}")
                                matches.append(
                                    Match(
                                        source="cursor-aiservice",
                                        session_id=f"{workspace_id}:{i}",
                                        sort_ts=db_mtime,
                                        display_ts=_format_ts(db_mtime),
                                        snippet=snippet,
                                        meta=meta,
                                        export_ref={
                                            "source": "cursor-aiservice",
                                            "db_path": str(db_path),
                                            "workspace_id": workspace_id,
                                            "index": i,
                                        },
                                    )
                                )

        # Global composers
        global_db = installation / "User" / "globalStorage" / "state.vscdb"
        if global_db.exists():
            global_mtime = global_db.stat().st_mtime
            try:
                conn = _connect_sqlite_ro(global_db)
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT key, value FROM cursorDiskKV
                    WHERE (key LIKE 'composerData:%' OR key LIKE 'bubbleId:%')
                      AND value IS NOT NULL
                      AND instr(value, ?) > 0
                    """,
                    (query,),
                )
                rows = cur.fetchall()
                conn.close()
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                rows = []

            for key, value in rows:
                if not isinstance(key, str) or not value:
                    continue

                if key.startswith("composerData:"):
                    try:
                        data = json.loads(value)
                    except Exception:
                        data = None
                    if not isinstance(data, dict):
                        continue

                    composer_id = data.get("composerId") or key.split(":", 1)[1]
                    name = data.get("name", "Untitled")
                    updated_at = data.get("lastUpdatedAt")
                    created_at = data.get("createdAt")
                    ts_value = updated_at or created_at or global_mtime
                    convo = data.get("conversation") or []

                    if isinstance(convo, list) and convo:
                        for bubble_idx, bubble in enumerate(convo):
                            if not isinstance(bubble, dict):
                                continue
                            text = bubble.get("text", "") or ""
                            haystack = text
                            if query not in haystack:
                                try:
                                    haystack = json.dumps(bubble, ensure_ascii=False)
                                except Exception:
                                    haystack = text
                            if query not in haystack:
                                continue

                            for idx in _iter_occurrences(haystack, query):
                                snippet = _make_snippet(haystack, idx, len(query), context_chars)
                                meta = _compact_one_line(f"composer={composer_id} name={name} bubble={bubble_idx} db={global_db}")
                                matches.append(
                                    Match(
                                        source="cursor-global-composer",
                                        session_id=str(composer_id),
                                        sort_ts=_to_sort_ts(ts_value),
                                        display_ts=_format_ts(ts_value),
                                        snippet=snippet,
                                        meta=meta,
                                        export_ref={
                                            "source": "cursor-global-composer",
                                            "db_path": str(global_db),
                                            "composer_id": composer_id,
                                        },
                                    )
                                )
                    else:
                        # Match in metadata or other fields; show a compact snippet of the record.
                        for idx in _iter_occurrences(value, query):
                            snippet = _make_snippet(value, idx, len(query), context_chars)
                            meta = _compact_one_line(f"composer={composer_id} name={name} db={global_db}")
                            matches.append(
                                Match(
                                    source="cursor-global-composer",
                                    session_id=str(composer_id),
                                    sort_ts=_to_sort_ts(ts_value),
                                    display_ts=_format_ts(ts_value),
                                    snippet=snippet,
                                    meta=meta,
                                    export_ref={
                                        "source": "cursor-global-composer",
                                        "db_path": str(global_db),
                                        "composer_id": composer_id,
                                    },
                                )
                            )

                elif key.startswith("bubbleId:"):
                    # bubbleId:{composer_id}:{bubble_id}
                    parts = key.split(":")
                    composer_id = parts[1] if len(parts) > 1 else None
                    bubble_id = parts[2] if len(parts) > 2 else None
                    if not composer_id:
                        continue
                    try:
                        bubble_data = json.loads(value)
                    except Exception:
                        bubble_data = None
                    if isinstance(bubble_data, dict):
                        text = bubble_data.get("text", "") or ""
                        haystack = text
                        if query not in haystack:
                            try:
                                haystack = json.dumps(bubble_data, ensure_ascii=False)
                            except Exception:
                                haystack = text
                        if query not in haystack:
                            continue
                        updated_at = bubble_data.get("createdAt") or bubble_data.get("timestamp") or global_mtime
                        for idx in _iter_occurrences(haystack, query):
                            snippet = _make_snippet(haystack, idx, len(query), context_chars)
                            meta = _compact_one_line(f"composer={composer_id} bubble={bubble_id} db={global_db}")
                            matches.append(
                                Match(
                                    source="cursor-global-composer",
                                    session_id=str(composer_id),
                                    sort_ts=_to_sort_ts(updated_at),
                                    display_ts=_format_ts(updated_at),
                                    snippet=snippet,
                                    meta=meta,
                                    export_ref={
                                        "source": "cursor-global-composer",
                                        "db_path": str(global_db),
                                        "composer_id": composer_id,
                                    },
                                )
                            )

    return matches


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _format_match_line(i: int, m: Match, query: str) -> str:
    session_id = m.session_id or "?"
    meta = _truncate(_tildeify(m.meta), 90)
    snippet = _truncate(_tildeify(m.snippet), 220)
    snippet = _highlight(snippet, _tildeify(query))

    line1 = f"[{i:>3}] {snippet}"
    line2_left = f"      {m.display_ts:>20} {m.source:<22} {session_id:<26}"
    line2 = f"{line2_left} {meta}" if meta else line2_left
    return f"{line1}\n{line2}"


def export_selected(match: Match, output_dir: Path, query: str) -> Path:
    q_piece = _safe_piece((query or "").strip()[:20], 20)

    def _base(prefix: str, *parts: Any) -> str:
        items: list[str] = [prefix]
        if q_piece:
            items.append(q_piece)
        for p in parts:
            if p is None:
                continue
            s = str(p).strip()
            if not s:
                continue
            items.append(s)
        return "_".join(items)

    source = match.export_ref.get("source")
    if source == "codex":
        import extract_codex

        session_file = Path(match.export_ref["session_file"])
        conv = extract_codex.extract_codex_session(session_file)
        if not conv:
            raise RuntimeError(f"Could not extract Codex session from {session_file}")
        base = _base("codex", conv.get("session_id") or session_file.stem)
        return _write_json_export(output_dir, base, conv)

    if source == "gemini-cli":
        import extract_gemini

        session_file = Path(match.export_ref["session_file"])
        conv = extract_gemini.extract_gemini_session(session_file)
        if not conv:
            raise RuntimeError(f"Could not extract Gemini session from {session_file}")
        base = _base("gemini", conv.get("session_id") or session_file.stem)
        return _write_json_export(output_dir, base, conv)

    if source == "opencode":
        import extract_opencode

        storage_base = Path(match.export_ref["storage_base"])
        session_id = match.export_ref.get("session_id")
        if not session_id:
            raise RuntimeError("OpenCode match did not include a session_id")

        session_file = None
        project_id = None
        session_root = storage_base / "session"
        if session_root.exists():
            for project_dir in session_root.iterdir():
                candidate = project_dir / f"{session_id}.json"
                if candidate.exists():
                    session_file = candidate
                    project_id = project_dir.name
                    break

        if session_file is None:
            session_file = storage_base / "session" / "unknown" / f"{session_id}.json"

        extractor = extract_opencode.CLIExtractor(storage_base)
        conv = extractor._extract_session(session_file, project_id)  # pylint: disable=protected-access
        if not conv:
            raise RuntimeError(f"Could not extract OpenCode session {session_id}")
        base = _base("opencode", session_id)
        return _write_json_export(output_dir, base, conv)

    if source == "cursor-chat":
        db_path = Path(match.export_ref["db_path"])
        conv = _cursor_export_chat(db_path, match.export_ref["workspace_id"], match.export_ref["tab_id"])
        if not conv:
            raise RuntimeError("Could not export Cursor chat session")
        base = _base("cursor_chat", match.export_ref["workspace_id"], match.export_ref["tab_id"])
        return _write_json_export(output_dir, base, conv)

    if source == "cursor-workspace-composer":
        db_path = Path(match.export_ref["db_path"])
        conv = _cursor_export_workspace_composer(
            db_path, match.export_ref["workspace_id"], match.export_ref["composer_id"]
        )
        if not conv:
            raise RuntimeError("Could not export Cursor workspace composer session")
        base = _base("cursor_ws_composer", match.export_ref["workspace_id"], match.export_ref["composer_id"])
        return _write_json_export(output_dir, base, conv)

    if source == "cursor-aiservice":
        db_path = Path(match.export_ref["db_path"])
        conv = _cursor_export_aiservice(db_path, match.export_ref["workspace_id"], int(match.export_ref["index"]))
        if not conv:
            raise RuntimeError("Could not export Cursor aiService conversation")
        base = _base("cursor_aiservice", match.export_ref["workspace_id"], match.export_ref["index"])
        return _write_json_export(output_dir, base, conv)

    if source == "cursor-global-composer":
        db_path = Path(match.export_ref["db_path"])
        conv = _cursor_export_global_composer(db_path, match.export_ref["composer_id"])
        if not conv:
            raise RuntimeError("Could not export Cursor global composer session")
        base = _base("cursor_composer", match.export_ref["composer_id"])
        return _write_json_export(output_dir, base, conv)

    raise RuntimeError(f"Unknown export source: {source}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search native session stores and export a selected session to the current directory."
    )
    parser.add_argument("query", nargs="?", help="Exact (case-sensitive) search string")
    parser.add_argument(
        "--tool",
        action="append",
        choices=["codex", "gemini", "opencode", "cursor"],
        help="Limit search to a tool (repeatable). Default: all.",
    )
    parser.add_argument("--context-chars", type=int, default=50, help="Snippet context characters (default: 50)")
    parser.add_argument(
        "--max-matches",
        type=int,
        default=None,
        help="After sorting, keep only the most recent N matches.",
    )
    parser.add_argument(
        "--no-rg",
        action="store_true",
        help="Disable ripgrep acceleration and fall back to Python scanning where applicable.",
    )

    args = parser.parse_args(argv)
    query = args.query
    if not query:
        query = input("Search query (case-sensitive): ").strip()
    if not query:
        print("No query provided.", file=sys.stderr)
        return 2

    tools = args.tool or ["codex", "gemini", "opencode", "cursor"]
    use_rg = not args.no_rg
    context_chars = max(0, int(args.context_chars))

    all_matches: list[Match] = []
    if "codex" in tools:
        all_matches.extend(search_codex(query, context_chars, use_rg=use_rg))
    if "gemini" in tools:
        all_matches.extend(search_gemini(query, context_chars, use_rg=use_rg))
    if "opencode" in tools:
        all_matches.extend(search_opencode_cli(query, context_chars, use_rg=use_rg))
    if "cursor" in tools:
        all_matches.extend(search_cursor(query, context_chars))

    if not all_matches:
        print("No matches found.")
        return 1

    # Oldest first; newest last. Unknown timestamps first.
    all_matches.sort(key=lambda m: (m.sort_ts is not None, m.sort_ts or 0.0))
    if args.max_matches and args.max_matches > 0 and len(all_matches) > args.max_matches:
        all_matches = all_matches[-args.max_matches :]

    for i, m in enumerate(all_matches, start=1):
        print(_format_match_line(i, m, query=query))

    selection = input("\nSelect entry number to export (blank to cancel): ").strip()
    if not selection:
        return 0
    if not selection.isdigit():
        print("Invalid selection (expected a number).", file=sys.stderr)
        return 2
    idx = int(selection)
    if idx < 1 or idx > len(all_matches):
        print("Selection out of range.", file=sys.stderr)
        return 2

    chosen = all_matches[idx - 1]
    output_dir = Path.cwd()
    out_path = export_selected(chosen, output_dir, query=query)
    print(f"\nExported to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
