# OpenCode Session Storage and JSONL Generation Guide

This document provides the technical details required to write a script that converts OpenCode session history into a `.jsonl` format identical to `gemini_conversations_*.jsonl`.

## 1. Data Locations

The default base path for storage is `~/.local/share/opencode/storage/` (standard XDG data directory).

## 2. File Hierarchy

OpenCode stores data in a flat JSON hierarchy to support granular updates:

- **Projects**: `project/[projectID].json`
  - `projectID` is the Git root commit hash or "global".
- **Sessions**: `session/[projectID]/[sessionID].json`
  - Fields: `id`, `projectID`, `title`, `time: { created: ms, updated: ms }`.
- **Messages**: `message/[sessionID]/[messageID].json`
  - Fields: `role` ("user" | "assistant"), `time: { created: ms }`, `modelID`, `agent`, `tokens: { input, output, cache, reasoning }`.
- **Parts**: `part/[messageID]/[partID].json`
  - The constituent pieces of a message.
  - Fields: `type` ("text" | "reasoning" | "tool"), `text` (content string), `metadata`.

## 3. Mapping to Target JSONL Format

To generate a line in the JSONL file, aggregate the data as follows:

| Target Field       | Source Location & Logic                                     |
| :----------------- | :---------------------------------------------------------- |
| **`session_id`**   | `session.id`                                                |
| **`project_hash`** | `session.projectID`                                         |
| **`start_time`**   | `session.time.created` (convert Unix ms to ISO 8601 string) |
| **`last_updated`** | `session.time.updated` (convert Unix ms to ISO 8601 string) |
| **`source`**       | Hardcoded to `"opencode"` or `"gemini-cli"`                 |
| **`messages`**     | Array of reconstructed messages (see below)                 |

### Reconstructing the `messages` Array

For each message in a session (sorted by `messageID`):

1.  **Metadata**: Map `role`, `timestamp` (converted to ISO), and `model` (from `modelID`).
2.  **`content`**:
    - Fetch all Parts associated with the `messageID`.
    - Concatenate the `text` field from all parts where `type === "text"`.
3.  **`thoughts`**:
    - Filter parts where `type === "reasoning"`.
    - Map to an object: `{ "subject": metadata.subject ?? "Thinking", "description": text, "timestamp": ms_to_iso }`.
4.  **`tokens`**:
    - Directly map the `tokens` object from the Message metadata JSON.

## 4. Implementation Algorithm

1.  **Crawl Projects**: List all files in `storage/project/` to get active project IDs.
2.  **Iterate Sessions**: For each project, list files in `storage/session/[projectID]/`.
3.  **Collect Messages**:
    - List files in `storage/message/[sessionID]/`.
    - **CRITICAL**: Sort files by filename (which are `Identifier.ascending` strings) to preserve conversation flow.
4.  **Aggregate Parts**:
    - For each message, list files in `storage/part/[messageID]/`.
    - Sort by part filename.
    - Filter and concatenate content based on the `type` field.
5.  **Export**: Serialize the session object to a single-line JSON and append to the output `.jsonl` file.

## 5. Implementation Notes

- **Timestamps**: OpenCode uses milliseconds (`number`). The output format expects ISO 8601 strings (`string`).
- **Tool Results**: Tool outputs are stored as `type: "tool"` parts. Depending on the desired JSONL cleanliness, you may want to skip these or summarize them in the `content` field.
- **IDs**: Use the filenames themselves as the IDs (minus the `.json` extension).
