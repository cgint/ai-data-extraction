I want to create a more advanced extraction mechanism. In this directory, you see Python files that extract history from different coding agents. So we know the locations and how to extract information from those. I want to have is a Python script where I can search within those JSON files. One option for this would be to use, for example, rg "looking to reduce latencies" /Users/cgint/.codex/sessions To directly search in sessions This would be the quickest one. What I would love, then, is that the user would be presented with a snippet around that searched query term with date and maybe some other metadata that could be relevant to choose the correct one.And then can select which one it wants to select. And then this specific session would be exported to the current directory the user is in. I would like this to work over the ones that I'm using. opencode, codex, gemini, cursor. Let's start with those to keep it rather simple. When it comes to OpenCode, I am only interested in the macOS CLI logs. So we can skip the Desktop: Tauri .dat files Mentioned. Please create a document where you take my input one-to-one as is and then analyze what it needs to do this. Put questions and most important information on top of the document while my instructions have always to be on the topmost position still.

---

## Questions (answer these first)

1. **What exactly should be exported?**
   - Raw source files from the agent’s native storage (e.g. `rollout-*.jsonl`, `session-*.json`)?
   - Or a normalized single-session JSON/JSONL export (similar to what the `extract_*.py` scripts produce)?
   - (OpenCode CLI sessions are fragmented across multiple files; exporting “raw” likely means exporting a directory/bundle.)
   - **User-Answer:** I would like to have a normalized single-session JSON/JSONL export (similar to what the `extract_*.py` scripts produce).
2. **Should Cursor be searched in native storage or only via extracted JSON?**
   - Cursor source storage is SQLite (`state.vscdb`), not JSON. If “JSON files only” is non-negotiable, Cursor needs a prior extraction step (or we treat `extracted_data/*.jsonl` as the searchable JSON).
   - **User-Answer:** I would like to search in the native storage of Cursor have a normalized single-session JSON/JSONL export.
3. **Search semantics:** literal vs regex, case sensitivity, and whether “match across JSON” should be constrained to message text fields (to avoid matching on keys/metadata).
   - **User-Answer:** Simple start full exact match case sensitive with no constraints for now.
5. **Result presentation:** how much snippet context (chars or lines), and do you want multiple matches per session summarized (count + best snippet) or only the first match?
   - **User-Answer:** Lets make it 50 chars before and after the match. Show every match - one match per line. Most recent matches at the bottom so that on terminal UI they are easier to see. Number each entry so that the user can select one.
6. **Selection & export UX:** single-select vs multi-select; should the script exit after exporting one session or allow exporting several in a row?
   - **User-Answer:** Single-select. The script should exit after exporting one session.

## Most important information (from the existing extractors in this repo)

- **Codex**
  - Storage: discovered under `~/.codex`, `~/.codex-local`, etc.
  - Sessions: `…/sessions/**/rollout-*.jsonl` (one session per file), plus possible `…/projects/**/*.jsonl` (`extract_codex.py`).
  - Metadata available: `session_meta` payload (`id`, `cwd`, `timestamp`), plus per-event `timestamp`.
- **Gemini CLI**
  - Sessions: `…/tmp/**/chats/session-*.json` (`extract_gemini.py`).
  - Metadata available: `sessionId`, `projectHash`, `startTime`, `lastUpdated`, plus per-message `timestamp`.
- **OpenCode (macOS CLI only)**
  - Storage (macOS): `~/Library/Application Support/opencode/storage` (`extract_opencode.py`).
  - Data layout: `storage/project/*.json`, `storage/session/<project_id>/*.json`, `storage/message/<session_id>/*.json`, `storage/part/<message_id>/*.json`.
  - Metadata available: session `time.created/updated` → `start_time/last_updated`, project `path` (cwd), `title`, `session_id`, plus per-message created times.
  - Note: message text often lives in `part/*.json` items (type `"text"`, `"code"`, `"reasoning"`, etc.).
- **Cursor**
  - Storage: SQLite DBs under `~/Library/Application Support/Cursor/User/...` (`extract_cursor.py`).
  - Workspace DBs: `…/User/workspaceStorage/<workspace_id>/state.vscdb`.
  - Global DB: `…/User/globalStorage/state.vscdb` with `cursorDiskKV` keys like `composerData:%` and `bubbleId:{composer_id}:{bubble_id}`.
  - Metadata: global composer objects include `createdAt` and `lastUpdatedAt`; other modes (chat/aiService) may not expose strong timestamps in the current extractor output.

## What the new script needs to do (behavioral requirements)

### Inputs

- Search query (string).
- Optional filters:
  - Tool(s): `codex`, `gemini`, `opencode`, `cursor`
  - Date range / “recent N days” (optional but likely useful)
  - Max results
  - Context size (e.g. `--context-chars 120` or `--context-lines 2`)

### Outputs / UX

1. Run a search across the chosen tool(s) session storage.
2. Group hits by “session” (tool + session identifier).
3. Display an interactive list of candidate sessions:
   - Snippet around the match
   - Date (prefer session start/last_updated; fallback to file mtime if needed)
   - Extra metadata that helps disambiguate:
     - `cwd` / project path
     - session id
     - project name (if available)
     - model/provider (if available and helpful)
     - match count per session (optional)
4. Allow the user to select a session (at minimum: numeric selection in the terminal).
5. Export the selected session into the current working directory.

## Proposed approaches (pick one for MVP)

### Option A — “Fast path” using `rg` for candidate discovery, then minimal parsing for metadata

- Use `rg` to find matching files quickly.
- Then parse only the corresponding session(s) in Python to:
  - extract accurate metadata (dates, ids, cwd)
  - generate clean “snippet around match” from message text (not raw JSON noise)
- Pros: very fast on large corpora; leverages your suggested workflow.
- Cons: still needs per-tool mapping logic (especially OpenCode and Cursor).

### Option B — “Normalized search” (search the extracted JSONL output)

- First (or periodically) run existing extractors to produce normalized JSONL in `extracted_data/`.
- Search those JSONL files and export the matching conversation line(s) as a session export.
- Pros: uniform format and metadata surface; avoids raw-format quirks.
- Cons: requires extraction to be up to date; not a direct search over native storage.

## Per-tool notes for mapping “match → session → export”

### Codex

- Likely simplest: each `rollout-*.jsonl` is already “the session”.
- `rg` hits can be grouped by file path.
- Metadata extraction:
  - scan file for the first `{"type":"session_meta",...}` event to get session id/cwd
  - fallback to file path date segments or file mtime for “date”
- Export:
  - copy the `rollout-*.jsonl` into CWD, or generate a normalized `session.json` export.

### Gemini CLI

- Each `session-*.json` is a session.
- Metadata is in the top-level JSON keys (`startTime`, `lastUpdated`, etc.).
- Export:
  - copy the `session-*.json` into CWD, or normalize into your common schema.

### OpenCode (macOS CLI)

- Search target:
  - message text is typically stored in `storage/part/<message_id>/*.json` (type `"text"`, `"code"`, `"reasoning"`, …).
- Mapping from a part hit to a session:
  - `part` path gives `message_id`
  - build a fast index at startup by walking `storage/message/<session_id>/*.json` and mapping `message_id → session_id` (directory name)
  - once you have `session_id`, you can load all messages/parts for export (or load the `storage/session/**/<session_id>.json` if present)
- Export:
  - **Normalized export** is simplest (one JSON file with messages + metadata).
  - If you want “raw export”, export a directory bundle containing:
    - `session/<project_id>/<session_id>.json` (if present)
    - `message/<session_id>/*.json`
    - `part/<message_id>/*.json` for all message ids in that session
    - `project/<project_id>.json` (for cwd/name)
- Explicitly exclude Desktop `.dat`/Tauri stores (per your request).

### Cursor

- Cursor is not JSON on disk; you have two practical MVP choices:
  1. **Search extracted JSONL** generated by `extract_cursor.py` (treat that as the “JSON files” you search).
  2. **Native search in SQLite**:
     - iterate relevant keys/tables (`ItemTable`, `cursorDiskKV`) and search text fields in Python
     - map match → conversation object (composer/chat tab/etc.)
- Export:
  - normalized conversation JSON (recommended), because copying the DB doesn’t isolate “a session”.

## Export format recommendation (for consistency)

- Export a single JSON file per selected session (one file in CWD), with:
  - `source` (`codex`, `gemini-cli`, `opencode`, `cursor`)
  - `session_id` (or best available identifier)
  - `start_time` / `last_updated` (when available)
  - `cwd` / project/workspace hints (when available)
  - `messages` (normalized with `role`, `content`, `timestamp`, plus tool-specific extras)
- This avoids per-tool “raw file bundle” complexity, while still meeting “export to current dir”.

## Minimal verification (once implemented)

- Manual checks (fast):
  - a query that matches in Codex sessions shows correct `cwd` and a reasonable snippet
  - a query that matches in Gemini sessions shows `lastUpdated` and exports the correct file
  - a query that matches in OpenCode part text maps to the correct `session_id` and exports only that session
  - Cursor path behaves according to the chosen approach (native DB search or extracted JSONL search)

