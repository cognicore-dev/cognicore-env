"""Provenance tables alongside CogniCore's existing memory and FTS tables."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS history_batches (
    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, platform TEXT NOT NULL,
    imported_at REAL NOT NULL, project TEXT NOT NULL, redact INTEGER NOT NULL,
    status TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS history_sources (
    id TEXT PRIMARY KEY, platform TEXT NOT NULL, original_id TEXT, project TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_sources_original ON history_sources (original_id);
CREATE TABLE IF NOT EXISTS history_messages (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES history_sources(id) ON DELETE CASCADE,
    entry_id INTEGER UNIQUE NOT NULL REFERENCES memory_entries(entry_id),
    event_time REAL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_messages_source ON history_messages (source_id, event_time);
CREATE TABLE IF NOT EXISTS history_batch_sources (
    batch_id TEXT REFERENCES history_batches(id) ON DELETE CASCADE,
    source_id TEXT REFERENCES history_sources(id) ON DELETE CASCADE,
    PRIMARY KEY (batch_id, source_id)
);
CREATE INDEX IF NOT EXISTS history_batch_sources_source ON history_batch_sources (source_id);
CREATE TABLE IF NOT EXISTS history_deleted_sources (
    batch_id TEXT REFERENCES history_batches(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL, PRIMARY KEY (batch_id, source_id)
);
CREATE TABLE IF NOT EXISTS history_batch_messages (
    batch_id TEXT REFERENCES history_batches(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES history_messages(id) ON DELETE CASCADE,
    PRIMARY KEY (batch_id, message_id)
);
CREATE INDEX IF NOT EXISTS history_batch_messages_message ON history_batch_messages (message_id);
CREATE TABLE IF NOT EXISTS history_claims (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES history_messages(id) ON DELETE CASCADE,
    entry_id INTEGER UNIQUE NOT NULL REFERENCES memory_entries(entry_id),
    kind TEXT NOT NULL, subject TEXT NOT NULL, group_key TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_claims_message ON history_claims (message_id);
CREATE INDEX IF NOT EXISTS history_claims_group ON history_claims (group_key, subject, kind);
CREATE TABLE IF NOT EXISTS history_relations (
    origin TEXT REFERENCES history_claims(id) ON DELETE CASCADE,
    target TEXT REFERENCES history_claims(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, PRIMARY KEY (origin, target, kind)
);
CREATE INDEX IF NOT EXISTS history_relations_target ON history_relations (target, kind);
CREATE TABLE IF NOT EXISTS history_evidence (
    claim_id TEXT REFERENCES history_claims(id) ON DELETE CASCADE,
    id TEXT, classification TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (claim_id, id)
);
CREATE TABLE IF NOT EXISTS history_audit (
    id INTEGER PRIMARY KEY, at REAL NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_sync_tombstones (
    kind TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY (kind, ref)
);
CREATE TABLE IF NOT EXISTS history_sync_aliases (
    alias TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES history_batches(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS history_sync_config (
    id INTEGER PRIMARY KEY CHECK (id=1), server TEXT NOT NULL, owner TEXT NOT NULL,
    enabled INTEGER NOT NULL, baseline TEXT NOT NULL DEFAULT '{}', last_sync REAL
);
CREATE TRIGGER IF NOT EXISTS history_remove_claim_index AFTER DELETE ON history_claims
BEGIN
    DELETE FROM memory_entries WHERE entry_id = OLD.entry_id;
END;
CREATE TRIGGER IF NOT EXISTS history_remove_message_index AFTER DELETE ON history_messages
BEGIN
    DELETE FROM memory_entries WHERE entry_id = OLD.entry_id;
END;
"""
