# Plan for `extract_opencode.py` Implementation

## Overview
Create a Python script `extract_opencode.py` to extract and normalize OpenCode session data into a JSONL format compatible with existing Gemini extraction scripts. This follows the data hierarchy and mapping logic defined in `opencode_session_log_info.md`.

## 1. Storage Architecture
OpenCode uses a flat JSON hierarchy:
- **Base Path**: `~/.local/share/opencode/storage/`
- **Structure**:
  - `project/`: Metadata about projects.
  - `session/[projectID]/`: Metadata about sessions.
  - `message/[sessionID]/`: Metadata about messages.
  - `part/[messageID]/`: Content parts (text, reasoning, tool).

## 2. Key Requirements
- **Ordering**: Filenames for messages and parts must be sorted lexicographically (e.g., using `sorted()`) to preserve conversation flow.
- **Timestamp Normalization**: Convert Unix milliseconds (OpenCode format) to ISO 8601 strings (Target format).
- **Content Aggregation**: Combine multiple "text" parts into a single `content` string for each message.
- **Reasoning Capture**: Map "reasoning" parts to a `thoughts` array with `subject`, `description`, and `timestamp`.
- **Token Tracking**: Directly map the `tokens` object from message metadata.

## 3. Implementation Plan

### Phase 1: Foundation
- Define `STORAGE_BASE` and implement OS-specific path discovery (Linux/macOS focus).
- Implement robust `load_json` and `get_sorted_json_files` utility functions.

### Phase 2: Traversal Logic
- Iterate through `storage/project/` to identify active `projectIDs`.
- For each project, traverse `storage/session/[projectID]/` to find sessions.
- For each session, gather messages from `storage/message/[sessionID]/`.
- For each message, aggregate parts from `storage/part/[messageID]/`.

### Phase 3: Normalization & Export
- Map OpenCode fields to the target JSONL schema:
  - `session_id`, `project_hash`, `start_time`, `last_updated`, `source` ("opencode"), `messages`.
- Reconstruct the `messages` array with normalized `role`, `content`, `thoughts`, and `tokens`.
- Implement JSONL export to `extracted_data/opencode_conversations_[timestamp].jsonl`.

### Phase 4: CLI & Statistics
- Provide real-time progress updates in the terminal.
- Output a summary of total sessions, messages, and token usage upon completion.

## 4. Todo List
- [ ] Initialize `extract_opencode.py` with required imports and constants.
- [ ] Implement storage path discovery for OpenCode.
- [ ] Implement hierarchical traversal (Project -> Session -> Message -> Part).
- [ ] Implement lexicographical sorting for messages and parts.
- [ ] Implement Unix MS to ISO 8601 conversion.
- [ ] Map OpenCode data to the target JSONL schema.
- [ ] Add summary statistics and console logging.
- [ ] Verify output compatibility with existing extraction data.
