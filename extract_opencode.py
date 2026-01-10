#!/usr/bin/env python3

import json
from pathlib import Path
from datetime import datetime
import os


def ms_to_iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0).isoformat()


def load_json(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def find_opencode_storage():
    home = Path.home()

    primary = home / ".local/share/opencode/storage"

    if primary.exists():
        return primary

    alternatives = [
        home / ".opencode/storage",
        home / "Library/Application Support/opencode/storage",
        Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
        / "opencode/storage",
    ]

    for alt in alternatives:
        if alt.exists():
            return alt

    return None


def get_sorted_items(directory):
    if not directory.exists():
        return []
    return sorted(list(directory.iterdir()), key=lambda x: x.name)


def extract_session(session_file, storage_base):
    session_data = load_json(session_file)
    if not session_data:
        return None

    session_id = session_data.get("id")
    project_id = session_data.get("projectID")

    if not session_id:
        return None

    messages_dir = storage_base / "message" / session_id
    if not messages_dir.exists():
        return None

    extracted_messages = []

    for message_file in get_sorted_items(messages_dir):
        if not message_file.suffix == ".json":
            continue

        msg_meta = load_json(message_file)
        if not msg_meta:
            continue

        message_id = message_file.stem
        parts_dir = storage_base / "part" / message_id

        content_parts = []
        thoughts = []

        if parts_dir.exists():
            for part_file in get_sorted_items(parts_dir):
                if not part_file.suffix == ".json":
                    continue

                part_data = load_json(part_file)
                if not part_data:
                    continue

                p_type = part_data.get("type")
                text = part_data.get("text", "")

                if p_type == "text":
                    content_parts.append(text)
                elif p_type == "reasoning":
                    metadata = part_data.get("metadata", {})
                    thoughts.append(
                        {
                            "subject": metadata.get("subject", "Thinking"),
                            "description": text,
                            "timestamp": ms_to_iso(
                                part_data.get("time", {}).get("created")
                            ),
                        }
                    )

        normalized_msg = {
            "role": msg_meta.get("role"),
            "content": "".join(content_parts),
            "timestamp": ms_to_iso(msg_meta.get("time", {}).get("created")),
            "model": msg_meta.get("modelID"),
            "agent": msg_meta.get("agent"),
            "tokens": msg_meta.get("tokens", {}),
        }

        if thoughts:
            normalized_msg["thoughts"] = thoughts

        extracted_messages.append(normalized_msg)

    if not extracted_messages:
        return None

    return {
        "session_id": session_id,
        "project_hash": project_id,
        "title": session_data.get("title"),
        "start_time": ms_to_iso(session_data.get("time", {}).get("created")),
        "last_updated": ms_to_iso(session_data.get("time", {}).get("updated")),
        "source": "opencode",
        "messages": extracted_messages,
        "source_file": str(session_file),
    }


def main():
    print("=" * 80)
    print("OPENCODE SESSION DATA EXTRACTION")
    print("=" * 80)
    print()

    storage_base = find_opencode_storage()
    if not storage_base:
        print("❌ OpenCode storage directory not found!")
        print("Expected at: ~/.local/share/opencode/storage/")
        return

    print(f"✅ Found storage: {storage_base}")

    projects_dir = storage_base / "project"
    if not projects_dir.exists():
        print("❌ Projects directory not found!")
        return

    all_sessions = []

    project_files = [f for f in projects_dir.iterdir() if f.suffix == ".json"]
    print(f"🔍 Found {len(project_files)} projects")

    for proj_file in project_files:
        project_id = proj_file.stem
        session_root = storage_base / "session" / project_id

        if not session_root.exists():
            continue

        session_files = [f for f in session_root.iterdir() if f.suffix == ".json"]
        print(
            f"📂 Processing project: {project_id[:8]}... ({len(session_files)} sessions)"
        )

        for sess_file in session_files:
            conv = extract_session(sess_file, storage_base)
            if conv:
                all_sessions.append(conv)

    print()
    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total sessions: {len(all_sessions):,}")

    if not all_sessions:
        print("No sessions found with valid messages!")
        return

    total_messages = sum(len(s["messages"]) for s in all_sessions)
    with_thoughts = sum(
        1 for s in all_sessions if any("thoughts" in m for m in s["messages"])
    )

    print(f"Total messages: {total_messages:,}")
    print(f"With thoughts: {with_thoughts:,}")
    print()

    output_dir = Path("extracted_data")
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"opencode_conversations_{timestamp}.jsonl"

    with open(output_file, "w", encoding="utf-8") as f:
        for conv in all_sessions:
            f.write(json.dumps(conv, ensure_ascii=False) + "\n")

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print(f"   Format: JSONL (one session per line)")


if __name__ == "__main__":
    main()
