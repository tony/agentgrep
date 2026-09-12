-- Synthetic global state.vscdb: one composer, one user turn, one header row.
CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
CREATE TABLE composerHeaders (
    composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER,
    lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER,
    recency INTEGER, checkpointAt INTEGER, value TEXT, subagentTypeName TEXT
);
INSERT INTO cursorDiskKV VALUES (
    'composerData:00000000-0000-4000-8000-00000000c0de',
    '{"composerId": "00000000-0000-4000-8000-00000000c0de", "name": "Refactor the parser"}'
);
INSERT INTO cursorDiskKV VALUES (
    'bubbleId:00000000-0000-4000-8000-00000000c0de:00000000-0000-4000-8000-0000000000b1',
    '{"type": 1, "text": "Split the tokenizer out of the parser"}'
);
INSERT INTO composerHeaders VALUES (
    '00000000-0000-4000-8000-00000000c0de', '0123456789abcdef0123456789abcdef',
    1779999600000, 1779999660000, 0, 0, 0, 0,
    '{"name": "Refactor the parser", "workspaceIdentifier": {"id": "0123456789abcdef0123456789abcdef", "uri": {"scheme": "vscode-remote", "authority": "wsl+Ubuntu", "path": "/home/user/work/project"}}}',
    NULL
);
