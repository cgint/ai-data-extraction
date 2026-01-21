#!/usr/bin/env python3
"""
Extract ALL OpenCode session data from all projects.

Supports:
- CLI: JSON files from ~/.local/share/opencode/storage (Linux) or
       ~/Library/Application Support/opencode/storage (macOS)
- Desktop: Tauri .dat files from ai.opencode.app directories

Includes: messages, reasoning/thoughts, tool calls, token usage, cost, model info
Auto-discovers OpenCode installations on the device.
"""

import json
import struct
import re
from pathlib import Path
from datetime import datetime
import platform
import os


# =============================================================================
# UTILITY HELPERS
# =============================================================================


def ms_to_iso(ms):
    """Convert milliseconds timestamp to ISO format string."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0).isoformat()


def load_json(file_path):
    """Safely load JSON file, returns None on any error."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_sorted_items(directory):
    """Get sorted list of items in a directory."""
    if not directory.exists():
        return []
    return sorted(list(directory.iterdir()), key=lambda x: x.name)


# =============================================================================
# METADATA RECONSTRUCTION (fallback when session files missing)
# =============================================================================


def extract_directory_from_content(text):
    """
    Try to extract a directory path from text content.
    Used as fallback when session metadata is missing.
    """
    if not text:
        return None

    # Pattern 1: cd command followed by path
    cd_pattern = r'cd\s+(["\']?)([^\s\'"]+)\1'
    matches = re.findall(cd_pattern, text)
    for match in matches:
        path = match[1] if isinstance(match, tuple) else match
        if path and (path.startswith("/") or path.startswith("~")):
            return path

    # Pattern 2: Common working directory indicators
    cwd_pattern = r"(?:working\s+)?directory[:\s]+([\"']?)([^\s\"']+)\1"
    matches = re.findall(cwd_pattern, text, re.IGNORECASE)
    for match in matches:
        path = match[1] if isinstance(match, tuple) else match
        if path and (path.startswith("/") or path.startswith("~")):
            return path

    return None


def extract_project_id_from_content(text):
    """
    Try to extract a project ID from text content.
    Used as fallback when session metadata is missing.
    """
    if not text:
        return None

    project_pattern = r"(?:project[-_]?id|project)[=:\s]+([a-zA-Z0-9_-]+)"
    match = re.search(project_pattern, text, re.IGNORECASE)
    if match:
        return match.group(1)

    return None


def generate_title_from_messages(messages):
    """Generate a title from the first user message."""
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            title = msg["content"][:100].strip()
            if len(msg["content"]) > 100:
                title += "..."
            return title
    return None


# =============================================================================
# INSTALLATION DISCOVERY
# =============================================================================


def find_opencode_installations():
    """
    Find all OpenCode installation directories.
    Returns list of tuples: (install_type, path) where type is 'cli' or 'desktop'.
    """
    system = platform.system()
    home = Path.home()

    installations = []

    # Define base directories per platform
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

    # Check for CLI installations
    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        # CLI: opencode/storage pattern
        opencode_storage = base_dir / "opencode/storage"
        if opencode_storage.exists():
            installations.append(("cli", opencode_storage))

    # Check for .opencode/storage in home
    alt_storage = home / ".opencode/storage"
    if alt_storage.exists():
        installations.append(("cli", alt_storage))

    # Check for Desktop (Tauri) installations
    if system == "Darwin":
        desktop_dirs = [home / "Library/Application Support/ai.opencode.app"]
    elif system == "Linux":
        desktop_dirs = [home / ".local/share/ai.opencode.app"]
    elif system == "Windows":
        desktop_dirs = [
            Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
            / "ai.opencode.app"
        ]
    else:
        desktop_dirs = []

    for desktop_dir in desktop_dirs:
        if desktop_dir.exists():
            installations.append(("desktop", desktop_dir))

    # Deduplicate
    seen = set()
    unique = []
    for install_type, path in installations:
        key = (install_type, str(path))
        if key not in seen:
            seen.add(key)
            unique.append((install_type, path))

    return unique


# =============================================================================
# CLI EXTRACTOR
# =============================================================================


class CLIExtractor:
    """Extracts conversations from OpenCode CLI JSON storage."""

    def __init__(self, storage_base):
        self.storage_base = storage_base

    def extract_all(self):
        """Extract all conversations from this CLI installation."""
        conversations = []

        sessions = self._find_all_sessions()
        for project_id, session_file in sessions:
            conv = self._extract_session(session_file, project_id)
            if conv:
                conversations.append(conv)

        return conversations

    def _find_all_sessions(self):
        """Find all session files in the storage directory."""
        sessions = []

        projects_dir = self.storage_base / "project"
        if not projects_dir.exists():
            return sessions

        project_files = [f for f in projects_dir.iterdir() if f.suffix == ".json"]

        for proj_file in project_files:
            project_id = proj_file.stem
            session_root = self.storage_base / "session" / project_id

            if not session_root.exists():
                continue

            session_files = [f for f in session_root.iterdir() if f.suffix == ".json"]
            for sess_file in session_files:
                sessions.append((project_id, sess_file))

        return sessions

    def _load_project_metadata(self, project_id):
        """Load project metadata (cwd, path, etc.) from project JSON."""
        if not project_id:
            return {}

        project_file = self.storage_base / "project" / f"{project_id}.json"
        data = load_json(project_file)
        if not data:
            return {}

        return {
            "cwd": data.get("path") or data.get("cwd"),
            "name": data.get("name"),
        }

    def _extract_message_parts(self, parts_dir):
        """
        Extract all parts for a message.
        Returns dict with: content, thoughts, tool_calls, tool_results, raw_content
        """
        result = {
            "content_parts": [],
            "thoughts": [],
            "tool_calls": [],
            "tool_results": [],
            "raw_content": [],  # For metadata reconstruction
        }

        if not parts_dir.exists():
            return result

        for part_file in get_sorted_items(parts_dir):
            if part_file.suffix != ".json":
                continue

            try:
                part_data = load_json(part_file)
                if not part_data:
                    continue

                p_type = part_data.get("type")
                text = part_data.get("text", "")

                # Collect raw content for potential metadata reconstruction
                if text:
                    result["raw_content"].append(text)

                if p_type == "text":
                    result["content_parts"].append(text)

                elif p_type == "reasoning":
                    metadata = part_data.get("metadata", {})
                    result["thoughts"].append(
                        {
                            "subject": metadata.get("subject", "Thinking"),
                            "description": text,
                            "timestamp": ms_to_iso(
                                part_data.get("time", {}).get("created")
                            ),
                        }
                    )

                elif p_type in ("tool", "tool-call"):
                    state = part_data.get("state", {})
                    tool_name = part_data.get("tool", part_data.get("name"))

                    tool_call = {
                        "id": part_data.get("callID", part_data.get("id")),
                        "name": tool_name,
                        "input": state.get("input", part_data.get("input")),
                    }

                    # If completed, include output
                    if state.get("status") == "completed" and "output" in state:
                        tool_call["output"] = state["output"]
                        result["tool_results"].append(
                            {
                                "tool_call_id": part_data.get("callID"),
                                "tool": tool_name,
                                "output": state["output"],
                            }
                        )

                    result["tool_calls"].append(tool_call)

                elif p_type == "tool-result":
                    result["tool_results"].append(
                        {
                            "tool_call_id": part_data.get("toolCallID"),
                            "output": part_data.get("output"),
                        }
                    )

                elif p_type == "code":
                    language = part_data.get("language", "")
                    code_text = part_data.get("text", "")
                    result["content_parts"].append(f"```{language}\n{code_text}\n```")

            except Exception:
                continue

        return result

    def _extract_session(self, session_file, project_id):
        """Extract a single session/conversation."""
        session_data = load_json(session_file)

        # Determine metadata source
        has_session_file = session_data is not None
        metadata_source = "session_file" if has_session_file else "reconstructed"

        if not session_data:
            session_data = {}

        session_id = session_data.get("id")
        if not session_id:
            # Try to get session ID from filename
            session_id = session_file.stem
            if not session_id.startswith("ses_"):
                return None

        messages_dir = self.storage_base / "message" / session_id
        if not messages_dir.exists():
            return None

        # Load project metadata
        project_meta = self._load_project_metadata(
            project_id or session_data.get("projectID")
        )

        extracted_messages = []
        all_raw_content = []

        for message_file in get_sorted_items(messages_dir):
            if message_file.suffix != ".json":
                continue

            try:
                msg_meta = load_json(message_file)
                if not msg_meta:
                    continue

                message_id = message_file.stem
                parts_dir = self.storage_base / "part" / message_id

                # Extract all parts
                parts = self._extract_message_parts(parts_dir)
                all_raw_content.extend(parts["raw_content"])

                # Build normalized message
                normalized_msg = {
                    "role": msg_meta.get("role"),
                    "content": "".join(parts["content_parts"]),
                    "timestamp": ms_to_iso(msg_meta.get("time", {}).get("created")),
                    "model": msg_meta.get("modelID"),
                    "agent": msg_meta.get("agent"),
                    "provider": msg_meta.get("providerID"),
                    "mode": msg_meta.get("mode"),
                    "cost": msg_meta.get("cost"),
                    "tokens": msg_meta.get("tokens", {}),
                }

                # Add optional fields only if present
                if parts["thoughts"]:
                    normalized_msg["thoughts"] = parts["thoughts"]
                if parts["tool_calls"]:
                    normalized_msg["tool_calls"] = parts["tool_calls"]
                if parts["tool_results"]:
                    normalized_msg["tool_results"] = parts["tool_results"]

                extracted_messages.append(normalized_msg)

            except Exception:
                continue

        if not extracted_messages:
            return None

        # Build conversation object
        conversation = {
            "session_id": session_id,
            "project_hash": project_id or session_data.get("projectID"),
            "cwd": project_meta.get("cwd"),
            "project_name": project_meta.get("name"),
            "title": session_data.get("title"),
            "start_time": ms_to_iso(session_data.get("time", {}).get("created")),
            "last_updated": ms_to_iso(session_data.get("time", {}).get("updated")),
            "version": session_data.get("version"),
            "source": "opencode",
            "metadata_source": metadata_source,
            "messages": extracted_messages,
            "source_file": str(session_file),
        }

        # Add parent session if present
        if session_data.get("parentID"):
            conversation["parent_session_id"] = session_data["parentID"]

        # Reconstruct missing metadata if needed
        if not has_session_file:
            combined_content = "\n".join(all_raw_content)
            if not conversation["cwd"]:
                conversation["cwd"] = extract_directory_from_content(combined_content)
            if not conversation["project_hash"]:
                conversation["project_hash"] = extract_project_id_from_content(
                    combined_content
                )
            if not conversation["title"]:
                conversation["title"] = generate_title_from_messages(extracted_messages)

            # Use first/last message times
            if extracted_messages:
                if not conversation["start_time"]:
                    conversation["start_time"] = extracted_messages[0].get("timestamp")
                if not conversation["last_updated"]:
                    conversation["last_updated"] = extracted_messages[-1].get(
                        "timestamp"
                    )

        return conversation


# =============================================================================
# DESKTOP (TAURI) EXTRACTOR
# =============================================================================


class DesktopExtractor:
    """Extracts conversations from OpenCode Desktop Tauri .dat files."""

    def __init__(self, desktop_dir):
        self.desktop_dir = desktop_dir

    def extract_all(self):
        """Extract all conversations from this Desktop installation."""
        conversations = []

        dat_files = list(self.desktop_dir.rglob("*.dat"))
        if not dat_files:
            return conversations

        for dat_file in dat_files:
            store = self._read_tauri_store(dat_file)
            if not store:
                continue

            # Look for session/conversation data in the store
            for key, value in store.items():
                if not isinstance(value, dict):
                    continue

                # Check if this looks like a conversation/session
                if "messages" in value or "history" in value:
                    try:
                        messages = value.get("messages", value.get("history", []))
                        if not messages:
                            continue

                        conversation = {
                            "messages": messages,
                            "source": "opencode-desktop",
                            "metadata_source": "tauri_store",
                            "store_key": key,
                            "store_file": str(dat_file.name),
                        }

                        # Add any additional metadata
                        for meta_key in [
                            "session_id",
                            "title",
                            "created_at",
                            "workspace",
                        ]:
                            if meta_key in value:
                                conversation[meta_key] = value[meta_key]

                        conversations.append(conversation)

                    except Exception:
                        continue

        return conversations

    def _read_tauri_store(self, dat_file):
        """
        Parse Tauri store .dat files.
        Format: Simple key-value pairs with length prefixes (4-byte little-endian).
        """
        try:
            with open(dat_file, "rb") as f:
                data = f.read()

            store = {}
            offset = 0

            while offset < len(data):
                # Read key length (4 bytes, little-endian)
                if offset + 4 > len(data):
                    break

                key_len = struct.unpack("<I", data[offset : offset + 4])[0]
                offset += 4

                # Sanity check
                if key_len > 10000 or offset + key_len > len(data):
                    break

                # Read key
                key = data[offset : offset + key_len].decode("utf-8", errors="ignore")
                offset += key_len

                # Read value length
                if offset + 4 > len(data):
                    break

                value_len = struct.unpack("<I", data[offset : offset + 4])[0]
                offset += 4

                # Sanity check
                if value_len > 10000000 or offset + value_len > len(data):
                    break

                # Read value
                try:
                    value_bytes = data[offset : offset + value_len]
                    value = json.loads(value_bytes.decode("utf-8"))
                    store[key] = value
                except Exception:
                    pass

                offset += value_len

            return store

        except Exception:
            return {}


# =============================================================================
# MAIN
# =============================================================================


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
        print()
        print("Searched locations:")
        print("  CLI: ~/.local/share/opencode/storage (Linux)")
        print("       ~/Library/Application Support/opencode/storage (macOS)")
        print("  Desktop: ~/.local/share/ai.opencode.app (Linux)")
        print("           ~/Library/Application Support/ai.opencode.app (macOS)")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for install_type, inst in installations:
        print(f"   - [{install_type}] {inst}")
    print()

    # Extract from all installations
    all_conversations = []
    installation_stats = {}

    for install_type, install_path in installations:
        print(f"📂 Processing [{install_type}]: {install_path}")

        if install_type == "cli":
            extractor = CLIExtractor(install_path)
        else:  # desktop
            extractor = DesktopExtractor(install_path)

        conversations = extractor.extract_all()

        if conversations:
            # Add installation info to each conversation
            for conv in conversations:
                conv["installation"] = str(install_path)

            all_conversations.extend(conversations)
            installation_stats[str(install_path)] = {
                "type": install_type,
                "count": len(conversations),
            }
            print(f"   ✅ {len(conversations)} conversations")
        else:
            print("   ⚠️  No conversations found")

    print()
    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total conversations: {len(all_conversations):,}")

    if not all_conversations:
        print("No conversations found!")
        return

    # Calculate statistics
    total_messages = sum(len(c.get("messages", [])) for c in all_conversations)

    with_thoughts = sum(
        1
        for c in all_conversations
        if any("thoughts" in m for m in c.get("messages", []))
    )

    with_tools = sum(
        1
        for c in all_conversations
        if any(
            "tool_calls" in m or "tool_results" in m for m in c.get("messages", [])
        )
    )

    with_cost = sum(
        1
        for c in all_conversations
        if any(m.get("cost") for m in c.get("messages", []))
    )

    complete = sum(
        1
        for c in all_conversations
        if any(m.get("role") == "assistant" for m in c.get("messages", []))
    )

    from_file = sum(
        1 for c in all_conversations if c.get("metadata_source") == "session_file"
    )
    reconstructed = sum(
        1 for c in all_conversations if c.get("metadata_source") == "reconstructed"
    )

    print(f"Complete (has assistant): {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With thoughts: {with_thoughts:,}")
    print(f"With tool use: {with_tools:,}")
    print(f"With cost data: {with_cost:,}")
    print(f"Metadata from file: {from_file:,}")
    print(f"Metadata reconstructed: {reconstructed:,}")
    print()

    print("Breakdown by installation:")
    for inst, stats in sorted(installation_stats.items(), key=lambda x: -x[1]["count"]):
        print(f"  [{stats['type']:7}] {Path(inst).name:30} {stats['count']:5,} conversations")
    print()

    # Save output
    output_dir = Path("extracted_data")
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"opencode_conversations_{timestamp}.jsonl"

    with open(output_file, "w", encoding="utf-8") as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + "\n")

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print("   Format: JSONL (one conversation per line)")


if __name__ == "__main__":
    main()
