#!/usr/bin/env python3
"""
Extract GitHub Copilot CLI ("Get Up") conversation/session data for training.

Primary data source (structured, preferred):
  ~/.copilot/session-state/**/*.jsonl
These JSONL files contain session events (user/assistant messages, tool requests/results, metadata).

Secondary / fallback source (summaries):
  ~/.copilot/history-session-state/session_*.json

Output:
  extracted_data/copilot_conversations_YYYYMMDD_HHMMSS.jsonl
  One conversation per line, aligned with other extract_*.py scripts in this repo.
"""

import json
import os
import platform
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _safe_json_loads(line: str) -> Optional[dict]:
    try:
        return json.loads(line)
    except Exception:
        return None


def _iso_to_epoch_ms(ts: Optional[str]) -> Optional[int]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Copilot uses ISO8601 with Z
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def find_copilot_installations() -> List[Path]:
    """
    Copilot CLI stores state under ~/.copilot (macOS/Linux) or %USERPROFILE%\\.copilot (Windows).
    Return any found installation roots.
    """
    system = platform.system()
    home = Path.home()

    candidates: List[Path] = []
    if system == "Windows":
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            candidates.append(Path(userprofile) / ".copilot")
        # Fallback
        candidates.append(home / ".copilot")
    else:
        candidates.append(home / ".copilot")

    installations = [p for p in candidates if p.exists() and p.is_dir()]
    # Deduplicate
    return list(dict.fromkeys(installations))


def _iter_session_event_jsonl_files(installation: Path) -> List[Path]:
    session_state = installation / "session-state"
    files: List[Path] = []
    if session_state.exists():
        # Both formats exist:
        # - session-state/<sessionId>.jsonl
        # - session-state/<randomId>/events.jsonl
        files.extend(list(session_state.rglob("*.jsonl")))
    return files


def _iter_history_session_json_files(installation: Path) -> List[Path]:
    hist = installation / "history-session-state"
    if not hist.exists():
        return []
    return list(hist.glob("session_*.json"))


@dataclass
class ParsedConversation:
    session_id: str
    conversation: Dict[str, Any]


def _add_tool_event_to_assistant(
    messages: List[Dict[str, Any]],
    assistant_idx_by_tool_call_id: Dict[str, int],
    tool_call_id: Optional[str],
    field: str,
    payload: Dict[str, Any],
) -> None:
    idx: Optional[int] = None
    if tool_call_id and tool_call_id in assistant_idx_by_tool_call_id:
        idx = assistant_idx_by_tool_call_id[tool_call_id]
    else:
        # fallback: last assistant message
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                idx = i
                break

    if idx is None:
        return

    if field not in messages[idx]:
        messages[idx][field] = []
    messages[idx][field].append(payload)


def parse_session_events_jsonl(path: Path) -> Optional[ParsedConversation]:
    """
    Parse a Copilot session event stream (JSONL) into a single conversation dict.
    """
    session_meta: Dict[str, Any] = {}
    messages: List[Dict[str, Any]] = []
    assistant_idx_by_tool_call_id: Dict[str, int] = {}

    session_id: Optional[str] = None
    start_time_iso: Optional[str] = None
    copilot_version: Optional[str] = None
    producer: Optional[str] = None
    selected_model: Optional[str] = None

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue

                ev = _safe_json_loads(line)
                if not isinstance(ev, dict):
                    continue

                ev_type = ev.get("type")
                data = ev.get("data") or {}
                timestamp = ev.get("timestamp")

                if ev_type == "session.start":
                    session_id = str(data.get("sessionId") or data.get("session_id") or session_id or "")
                    start_time_iso = data.get("startTime") or timestamp
                    copilot_version = data.get("copilotVersion")
                    producer = data.get("producer")
                    selected_model = data.get("selectedModel")
                    session_meta.update(
                        {
                            "session_id": session_id,
                            "copilot_version": copilot_version,
                            "producer": producer,
                            "selected_model": selected_model,
                            "start_time": start_time_iso,
                        }
                    )

                elif ev_type == "session.model_change":
                    selected_model = data.get("newModel") or selected_model
                    session_meta["selected_model"] = selected_model

                elif ev_type == "user.message":
                    msg: Dict[str, Any] = {
                        "role": "user",
                        "content": (data.get("content") or ""),
                        "timestamp": timestamp,
                    }
                    if data.get("attachments"):
                        msg["attachments"] = data["attachments"]
                    messages.append(msg)

                elif ev_type == "assistant.message":
                    msg: Dict[str, Any] = {
                        "role": "assistant",
                        "content": (data.get("content") or ""),
                        "timestamp": timestamp,
                    }
                    # toolRequests: list[{toolCallId,name,arguments}]
                    tool_requests = data.get("toolRequests") or []
                    if tool_requests:
                        msg["tool_requests"] = tool_requests
                        # link toolCallId -> this assistant message index for later tool results
                        for tr in tool_requests:
                            tcid = tr.get("toolCallId")
                            if tcid:
                                assistant_idx_by_tool_call_id[str(tcid)] = len(messages)
                    # model may be present in some versions
                    if data.get("model"):
                        msg["model"] = data["model"]
                    if data.get("messageId"):
                        msg["message_id"] = data["messageId"]
                    messages.append(msg)

                elif ev_type == "tool.execution_start":
                    tool_call_id = data.get("toolCallId")
                    payload = {
                        "type": "tool.execution_start",
                        "toolCallId": tool_call_id,
                        "toolName": data.get("toolName"),
                        "arguments": data.get("arguments"),
                        "timestamp": timestamp,
                    }
                    _add_tool_event_to_assistant(
                        messages=messages,
                        assistant_idx_by_tool_call_id=assistant_idx_by_tool_call_id,
                        tool_call_id=str(tool_call_id) if tool_call_id else None,
                        field="tool_use",
                        payload=payload,
                    )

                elif ev_type == "tool.execution_complete":
                    tool_call_id = data.get("toolCallId")
                    payload = {
                        "type": "tool.execution_complete",
                        "toolCallId": tool_call_id,
                        "toolName": data.get("toolName"),
                        "success": data.get("success"),
                        "result": data.get("result"),
                        "timestamp": timestamp,
                    }
                    _add_tool_event_to_assistant(
                        messages=messages,
                        assistant_idx_by_tool_call_id=assistant_idx_by_tool_call_id,
                        tool_call_id=str(tool_call_id) if tool_call_id else None,
                        field="tool_results",
                        payload=payload,
                    )

                # Keep other session/tool event types for possible debugging, but don't emit as messages.

    except Exception:
        return None

    # Determine session_id if missing (fall back to filename)
    if not session_id:
        # session-state/<sessionId>.jsonl
        # session-state/<random>/events.jsonl
        if path.name == "events.jsonl" and path.parent.name:
            session_id = path.parent.name
        else:
            session_id = path.stem

    if not messages:
        return None

    created_at = _iso_to_epoch_ms(start_time_iso) or _iso_to_epoch_ms(messages[0].get("timestamp"))

    conv: Dict[str, Any] = {
        "messages": messages,
        "source": "copilot-cli",
        "session_id": session_id,
        "created_at": created_at,
        "source_file": str(path),
    }
    # Add compact metadata (avoid exploding output size)
    if copilot_version:
        conv["copilot_version"] = copilot_version
    if producer:
        conv["producer"] = producer
    if selected_model:
        conv["selected_model"] = selected_model

    # Basic flags like other scripts
    conv["has_tools"] = any(
        ("tool_use" in m or "tool_results" in m or "tool_requests" in m) for m in messages
    )
    conv["complete"] = any(m.get("role") == "assistant" and (m.get("content") or m.get("tool_requests")) for m in messages)

    return ParsedConversation(session_id=session_id, conversation=conv)


def parse_history_session_json(path: Path) -> Optional[ParsedConversation]:
    """
    Parse Copilot history session summary JSON (fallback).
    This file contains `chatMessages` with roles user/assistant/tool in an OpenAI-ish shape.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return None

    session_id = obj.get("sessionId")
    if not session_id:
        # filename: session_<uuid>_<timestamp>.json
        name = path.name
        if name.startswith("session_"):
            parts = name.split("_")
            if len(parts) >= 2:
                session_id = parts[1]
    if not session_id:
        return None

    chat_messages = obj.get("chatMessages") or []
    if not isinstance(chat_messages, list) or not chat_messages:
        return None

    messages: List[Dict[str, Any]] = []
    for m in chat_messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user":
            messages.append({"role": "user", "content": m.get("content", "")})
        elif role == "assistant":
            out: Dict[str, Any] = {"role": "assistant", "content": m.get("content", "")}
            if "tool_calls" in m:
                out["tool_calls"] = m["tool_calls"]
            messages.append(out)
        elif role == "tool":
            # Attach to prior assistant message if possible
            payload = {
                "type": "tool_message",
                "tool_call_id": m.get("tool_call_id"),
                "content": m.get("content", ""),
            }
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "assistant":
                    messages[i].setdefault("tool_results", []).append(payload)
                    break

    if not messages:
        return None

    conv: Dict[str, Any] = {
        "messages": messages,
        "source": "copilot-cli",
        "session_id": session_id,
        "created_at": _iso_to_epoch_ms(obj.get("startTime")),
        "source_file": str(path),
        "has_tools": any(("tool_calls" in m or "tool_results" in m) for m in messages),
        "complete": any(m.get("role") == "assistant" and m.get("content") for m in messages),
    }
    return ParsedConversation(session_id=session_id, conversation=conv)


def main() -> None:
    print("=" * 80)
    print("GITHUB COPILOT CLI (GET UP) DATA EXTRACTION")
    print("=" * 80)
    print()

    installations = find_copilot_installations()
    if not installations:
        print("❌ No Copilot CLI installation found (expected ~/.copilot)")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    all_conversations: List[Dict[str, Any]] = []
    by_session_id: Dict[str, Dict[str, Any]] = {}
    stats = defaultdict(int)

    for installation in installations:
        print(f"📂 Processing: {installation}")

        # Preferred: session-state events jsonl
        event_files = _iter_session_event_jsonl_files(installation)
        print(f"   Found {len(event_files)} session event file(s)")

        for ef in event_files:
            parsed = parse_session_events_jsonl(ef)
            if not parsed:
                continue
            # Prefer structured session-state over history
            by_session_id[parsed.session_id] = parsed.conversation
            stats["session_state"] += 1

        # Fallback: history summaries (only for missing session_ids)
        hist_files = _iter_history_session_json_files(installation)
        print(f"   Found {len(hist_files)} history session file(s)")
        for hf in hist_files:
            parsed = parse_history_session_json(hf)
            if not parsed:
                continue
            if parsed.session_id not in by_session_id:
                by_session_id[parsed.session_id] = parsed.conversation
                stats["history_state"] += 1

    all_conversations = list(by_session_id.values())

    print()
    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total conversations: {len(all_conversations):,}")
    print(f"  From session-state: {stats['session_state']:,}")
    print(f"  From history-session-state (fallback): {stats['history_state']:,}")

    if not all_conversations:
        print("No conversations found!")
        return

    total_messages = sum(len(c.get("messages", [])) for c in all_conversations)
    complete = sum(1 for c in all_conversations if c.get("complete"))
    with_tools = sum(1 for c in all_conversations if c.get("has_tools"))
    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With tool use/results: {with_tools:,}")
    print()

    # Save JSONL
    output_dir = Path("extracted_data")
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"copilot_conversations_{timestamp}.jsonl"

    with open(output_file, "w", encoding="utf-8") as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + "\n")

    file_size_mb = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size_mb:.2f} MB")
    print("   Format: JSONL (one conversation per line)")


if __name__ == "__main__":
    main()

