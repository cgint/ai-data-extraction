#!/usr/bin/env python3
"""
Extract ALL Cursor data: Chat, Composer, Agent, Tabs
Includes: messages, code context, diffs, file references, suggested edits
Auto-discovers Cursor installations on the device
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime
import platform
import os
from collections import defaultdict

def find_cursor_installations():
    """Find all Cursor installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []

    if system == "Darwin":  # macOS
        base_dirs = [
            home / "Library/Application Support",
            home / ".config"
        ]
    elif system == "Linux":
        base_dirs = [
            home / ".config",
            home / ".local/share"
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')),
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local'))
        ]
    else:
        base_dirs = [home / ".config"]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        cursor_dir = base_dir / 'Cursor'
        if cursor_dir.exists():
            locations.append(cursor_dir)

    return list(set(locations))

def extract_chat_mode(db_path, workspace_id):
    """Extract old-style Chat conversations with full context"""
    conversations = []

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM ItemTable WHERE key = 'workbench.panel.aichat.view.aichat.chatdata'")
        result = cursor.fetchone()

        if result:
            data = json.loads(result[0])
            if 'tabs' in data:
                for tab in data['tabs']:
                    if 'bubbles' in tab and len(tab['bubbles']) > 0:
                        messages = []
                        code_context = []
                        suggested_diffs = []

                        for bubble in tab['bubbles']:
                            bubble_type = bubble.get('type')
                            content = bubble.get('rawText', bubble.get('text', ''))

                            msg = {
                                'role': 'user' if bubble_type == 'user' else 'assistant',
                                'content': content
                            }

                            # Extract code context
                            if 'selections' in bubble and bubble['selections']:
                                ctx = []
                                for sel in bubble['selections']:
                                    if 'uri' in sel and 'fsPath' in sel['uri']:
                                        ctx.append({
                                            'file': sel['uri']['fsPath'],
                                            'code': sel.get('text', sel.get('rawText', '')),
                                            'range': sel.get('range')
                                        })
                                if ctx:
                                    msg['code_context'] = ctx
                                    code_context.extend(ctx)

                            # Extract suggested diffs
                            if 'suggestedDiffs' in bubble and bubble['suggestedDiffs']:
                                msg['suggested_diffs'] = bubble['suggestedDiffs']
                                suggested_diffs.extend(bubble['suggestedDiffs'])

                            messages.append(msg)

                        if messages:
                            conversations.append({
                                'messages': messages,
                                'source': 'cursor-chat',
                                'chat_title': tab.get('chatTitle'),
                                'tab_id': tab.get('tabId'),
                                'workspace_id': workspace_id,
                                'has_code_context': len(code_context) > 0,
                                'has_diffs': len(suggested_diffs) > 0
                            })

        conn.close()
    except Exception as e:
        print(f"Error extracting chat from {db_path}: {e}")

    return conversations

def extract_composer_conversations(global_db_path):
    """Extract Composer/Agent conversations from global storage"""
    conversations = []

    try:
        conn = sqlite3.connect(global_db_path)
        cursor = conn.cursor()

        # Get all composerData entries
        cursor.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
        results = cursor.fetchall()

        for key, value in results:
            if not value:
                continue

            try:
                data = json.loads(value)

                if 'conversation' in data and len(data['conversation']) > 0:
                    composer_id = data.get('composerId', key.split(':')[1])

                    messages = []
                    code_contexts = []
                    diffs = []

                    for bubble in data['conversation']:
                        bubble_type = bubble.get('type')
                        text = bubble.get('text', '')

                        if bubble_type == 1:  # User message
                            msg = {
                                'role': 'user',
                                'content': text
                            }

                            # Extract code context
                            context = bubble.get('context', {})
                            if context and 'selections' in context:
                                ctx = []
                                for sel in context['selections']:
                                    if 'uri' in sel and 'fsPath' in sel['uri']:
                                        ctx.append({
                                            'file': sel['uri']['fsPath'],
                                            'code': sel.get('text', sel.get('rawText', '')),
                                            'range': sel.get('range')
                                        })
                                if ctx:
                                    msg['code_context'] = ctx
                                    code_contexts.extend(ctx)

                            messages.append(msg)

                        elif bubble_type == 2:  # AI response
                            msg = {
                                'role': 'assistant',
                                'content': text
                            }

                            # Extract code blocks
                            if 'codeBlocks' in bubble and bubble['codeBlocks']:
                                msg['code_blocks'] = bubble['codeBlocks']

                            # Extract suggested diffs
                            if 'suggestedCodeBlocks' in bubble and bubble['suggestedCodeBlocks']:
                                msg['suggested_code_blocks'] = bubble['suggestedCodeBlocks']
                                diffs.extend(bubble['suggestedCodeBlocks'])

                            # Extract diff histories
                            if 'diffHistories' in bubble and bubble['diffHistories']:
                                msg['diff_histories'] = bubble['diffHistories']
                                diffs.extend(bubble['diffHistories'])

                            messages.append(msg)

                    if messages:
                        conversations.append({
                            'messages': messages,
                            'source': 'cursor-composer',
                            'composer_id': composer_id,
                            'name': data.get('name', 'Untitled'),
                            'status': data.get('status'),
                            'unified_mode': data.get('unifiedMode'),  # agent or chat
                            'created_at': data.get('createdAt'),
                            'updated_at': data.get('lastUpdatedAt'),
                            'has_code_context': len(code_contexts) > 0,
                            'has_diffs': len(diffs) > 0
                        })

            except (json.JSONDecodeError, KeyError) as e:
                continue

        conn.close()
    except Exception as e:
        print(f"Error extracting composer from {global_db_path}: {e}")

    return conversations

def main():
    print("="*80)
    print("CURSOR COMPLETE DATA EXTRACTION (Chat + Composer + Agent)")
    print("="*80)
    print()

    # Find all Cursor installations
    print("🔍 Searching for Cursor installations...")
    installations = find_cursor_installations()

    if not installations:
        print("❌ No Cursor installations found!")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    all_conversations = []
    stats = defaultdict(int)

    for installation in installations:
        print(f"📂 Processing: {installation}")

        # Extract Chat mode (workspace storage)
        workspace_storage = installation / 'User/workspaceStorage'
        if workspace_storage.exists():
            workspace_count = 0
            for workspace in workspace_storage.iterdir():
                if workspace.is_dir() and workspace.name != 'ext-dev':
                    db_file = workspace / 'state.vscdb'
                    if db_file.exists():
                        convs = extract_chat_mode(db_file, workspace.name)
                        all_conversations.extend(convs)
                        workspace_count += len(convs)

            print(f"   ✅ Chat mode: {workspace_count} conversations")
            stats['chat'] += workspace_count

        # Extract Composer/Agent mode (global storage)
        global_storage = installation / 'User/globalStorage/state.vscdb'
        if global_storage.exists():
            convs = extract_composer_conversations(global_storage)
            all_conversations.extend(convs)
            print(f"   ✅ Composer/Agent: {len(convs)} conversations")
            stats['composer'] += len(convs)
        else:
            print(f"   ⚠️  No global storage found")

    print()
    print("="*80)
    print("EXTRACTION COMPLETE")
    print("="*80)
    print(f"Total conversations: {len(all_conversations):,}")
    print(f"  Chat mode: {stats['chat']:,}")
    print(f"  Composer/Agent: {stats['composer']:,}")

    if not all_conversations:
        print("No conversations found!")
        return

    # Statistics
    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_code = sum(1 for c in all_conversations if c.get('has_code_context'))
    with_diffs = sum(1 for c in all_conversations if c.get('has_diffs'))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With code context: {with_code:,}")
    print(f"With diffs: {with_diffs:,}")
    print()

    # Save to organized JSONL
    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'cursor_complete_{timestamp}.jsonl'

    with open(output_file, 'w') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print(f"   Format: JSONL (one conversation per line)")

if __name__ == '__main__':
    main()
