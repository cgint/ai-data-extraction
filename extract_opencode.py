#!/usr/bin/env python3
"""
Extract ALL OpenCode session data from all projects
Includes: messages, reasoning metadata, token usage, model info
Auto-discovers OpenCode installations on the device
"""

import json
from pathlib import Path
from datetime import datetime
import platform
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


def find_opencode_installations():
    """Find all OpenCode installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []

    if system == "Darwin":  # macOS
        base_dirs = [
            home / "Library/Application Support",
            home / ".config",
            home / ".local/share",
        ]
    elif system == "Linux":
        base_dirs = [
            home / ".config",
            home / ".local/share",
            Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")),
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get("APPDATA", home / "AppData/Roaming")),
            Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")),
        ]
    else:
        base_dirs = [home / ".config", home / ".local/share"]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        # Check for opencode/storage pattern
        opencode_storage = base_dir / "opencode/storage"
        if opencode_storage.exists():
            locations.append(opencode_storage)

        # Also check for .opencode/storage in home
        if base_dir == home:
            alt_storage = home / ".opencode/storage"
            if alt_storage.exists():
                locations.append(alt_storage)

    return list(set(locations))


def get_sorted_items(directory):
    if not directory.exists():
        return []
    return sorted(list(directory.iterdir()), key=lambda x: x.name)


def load_project_metadata(storage_base, project_id):
    """Load project metadata (cwd, path, etc.) from project JSON"""
    project_file = storage_base / "project" / f"{project_id}.json"
    data = load_json(project_file)
    if not data:
        return {}
    return {
        "cwd": data.get("path") or data.get("cwd"),
        "name": data.get("name"),
    }


def find_all_opencode_sessions(storage_base):
    """Find all session files in an OpenCode storage directory"""
    sessions = []

    projects_dir = storage_base / "project"
    if not projects_dir.exists():
        return sessions

    project_files = [f for f in projects_dir.iterdir() if f.suffix == ".json"]

    for proj_file in project_files:
        project_id = proj_file.stem
        session_root = storage_base / "session" / project_id

        if not session_root.exists():
            continue

        session_files = [f for f in session_root.iterdir() if f.suffix == ".json"]
        for sess_file in session_files:
            sessions.append((project_id, sess_file))

    return sessions


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

    # Load project metadata for cwd/name
    project_meta = load_project_metadata(storage_base, project_id) if project_id else {}

    extracted_messages = []

    for message_file in get_sorted_items(messages_dir):
        if not message_file.suffix == ".json":
            continue

        try:
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

                    try:
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
                    except Exception:
                        continue  # Skip bad part, keep other parts

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
        except Exception:
            continue  # Skip bad message, keep other messages

    if not extracted_messages:
        return None

    return {
        "session_id": session_id,
        "project_hash": project_id,
        "cwd": project_meta.get("cwd"),
        "project_name": project_meta.get("name"),
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

    # Find all OpenCode installations
    print("🔍 Searching for OpenCode installations...")
    installations = find_opencode_installations()

    if not installations:
        print("❌ No OpenCode installations found!")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    # Extract from all installations
    all_sessions = []
    installation_stats = {}

    for installation in installations:
        print(f"📂 Processing: {installation}")

        sessions = find_all_opencode_sessions(installation)
        print(f"   Found {len(sessions)} session files")

        # Group sessions by project for better reporting
        project_counts = {}
        conversations = []

        for project_id, sess_file in sessions:
            project_counts[project_id] = project_counts.get(project_id, 0) + 1
            conv = extract_session(sess_file, installation)
            if conv:
                conv["installation"] = str(installation)
                conversations.append(conv)

        if conversations:
            all_sessions.extend(conversations)
            installation_stats[str(installation)] = len(conversations)

            # Print project breakdown
            for project_id, count in sorted(project_counts.items()):
                print(
                    f"   📂 Processing project: {project_id[:8]}... ({count} sessions)"
                )

            print(f"   ✅ {len(conversations)} conversations")
        else:
            print("   ⚠️  No conversations found")

    print()
    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total conversations: {len(all_sessions):,}")

    if not all_sessions:
        print("No conversations found!")
        return

    # Statistics
    total_messages = sum(len(s["messages"]) for s in all_sessions)
    with_thoughts = sum(
        1 for s in all_sessions if any("thoughts" in m for m in s["messages"])
    )
    complete = sum(
        1
        for s in all_sessions
        if any(m["role"] == "assistant" for m in s["messages"])
    )

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With thoughts: {with_thoughts:,}")
    print()

    print("Breakdown by installation:")
    for inst, count in sorted(installation_stats.items(), key=lambda x: -x[1]):
        print(f"  {Path(inst).name:20} {count:5,} conversations")
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
    print("   Format: JSONL (one conversation per line)")


if __name__ == "__main__":
    main()
