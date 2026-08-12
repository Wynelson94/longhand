-- Genuine v0.11.2 schema: produced by executing that tag's own SCHEMA and
-- apply_migrations() (migrations 1-6, _apply_alters included), then seeding
-- one project / one session / two events (one of them a file edit).
-- Regeneration recipe lives in tests/test_db_compat.py. Do NOT hand-edit.
BEGIN TRANSACTION;
CREATE TABLE conversation_segments (
        segment_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        project_id TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        start_sequence INTEGER NOT NULL,
        end_sequence INTEGER NOT NULL,
        segment_type TEXT NOT NULL,
        topic TEXT NOT NULL,
        summary TEXT NOT NULL,
        event_count INTEGER NOT NULL,
        user_message_count INTEGER NOT NULL,
        keywords_json TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
    );
CREATE TABLE episodes (
        episode_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        project_id TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        problem_event_id TEXT,
        diagnosis_event_id TEXT,
        fix_event_id TEXT,
        verification_event_id TEXT,
        problem_description TEXT,
        diagnosis_summary TEXT,
        fix_summary TEXT,
        touched_files_json TEXT,
        tags_json TEXT,
        confidence REAL DEFAULT 0.5,
        status TEXT DEFAULT 'unresolved'
    , fix_commit_hash TEXT);
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_event_id TEXT,
    event_type TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    cwd TEXT,
    git_branch TEXT,
    model TEXT,
    content TEXT NOT NULL,
    is_sidechain INTEGER DEFAULT 0,
    tool_name TEXT,
    tool_use_id TEXT,
    tool_input_json TEXT,
    tool_output TEXT,
    tool_success INTEGER,
    file_path TEXT,
    file_operation TEXT,
    old_content TEXT,
    new_content TEXT,
    error_detected INTEGER DEFAULT 0,
    error_snippet TEXT,
    raw_json TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
INSERT INTO "events" VALUES('ev_fixture_0001','11111111-2222-3333-4444-555555555555',NULL,'user_message',0,'2026-06-18T00:00:30+00:00',NULL,NULL,NULL,'the fixture question',0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,NULL,'{"type":"user"}');
INSERT INTO "events" VALUES('ev_fixture_0002','11111111-2222-3333-4444-555555555555',NULL,'tool_call',1,'2026-06-18T00:00:45+00:00',NULL,NULL,NULL,'edited the file',0,'Edit',NULL,NULL,NULL,NULL,'/tmp/fixture-project/main.py','edit',NULL,NULL,0,NULL,'{"type":"assistant"}');
CREATE TABLE git_operations (
        git_op_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        operation_type TEXT NOT NULL,
        commit_hash TEXT,
        commit_message TEXT,
        branch TEXT,
        remote TEXT,
        files_changed_count INTEGER,
        timestamp TEXT NOT NULL,
        success INTEGER NOT NULL DEFAULT 1
    );
CREATE TABLE ingestion_log (
    transcript_path TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    event_count INTEGER NOT NULL
, last_offset INTEGER NOT NULL DEFAULT 0);
CREATE TABLE projects (
        project_id TEXT PRIMARY KEY,
        canonical_path TEXT NOT NULL,
        display_name TEXT NOT NULL,
        aliases_json TEXT NOT NULL,
        keywords_json TEXT NOT NULL,
        languages_json TEXT NOT NULL,
        category TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        session_count INTEGER DEFAULT 0,
        total_edits INTEGER DEFAULT 0
    );
INSERT INTO "projects" VALUES('p_fixture0001','/tmp/fixture-project','fixture-project','[]','[]','[]','tool','2026-06-18T00:00:00+00:00','2026-06-18T01:00:00+00:00',1,1);
CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
INSERT INTO "schema_version" VALUES(1,'2026-08-12T11:05:33.900402');
INSERT INTO "schema_version" VALUES(2,'2026-08-12T11:05:33.901656');
INSERT INTO "schema_version" VALUES(3,'2026-08-12T11:05:33.902947');
INSERT INTO "schema_version" VALUES(4,'2026-08-12T11:05:33.903132');
INSERT INTO "schema_version" VALUES(5,'2026-08-12T11:05:33.903788');
INSERT INTO "schema_version" VALUES(6,'2026-08-12T11:05:33.903987');
CREATE TABLE session_outcomes (
        session_id TEXT PRIMARY KEY,
        outcome TEXT NOT NULL,
        confidence REAL NOT NULL,
        error_count INTEGER DEFAULT 0,
        fix_count INTEGER DEFAULT 0,
        test_pass_count INTEGER DEFAULT 0,
        test_fail_count INTEGER DEFAULT 0,
        first_error_event_id TEXT,
        resolution_event_id TEXT,
        summary TEXT NOT NULL,
        topics_json TEXT NOT NULL,
        computed_at TEXT NOT NULL
    );
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    project_path TEXT,
    transcript_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    event_count INTEGER DEFAULT 0,
    user_message_count INTEGER DEFAULT 0,
    assistant_message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    file_edit_count INTEGER DEFAULT 0,
    git_branch TEXT,
    cwd TEXT,
    model TEXT,
    ingested_at TEXT NOT NULL
, project_id TEXT);
INSERT INTO "sessions" VALUES('11111111-2222-3333-4444-555555555555','/tmp/fixture-project','/tmp/fixture.jsonl','2026-06-18T00:00:00+00:00','2026-06-18T01:00:00+00:00',2,0,0,0,0,NULL,NULL,NULL,'2026-06-18T01:05:00+00:00','p_fixture0001');
CREATE TABLE tool_pairs (
        tool_use_id TEXT PRIMARY KEY,
        call_event_id TEXT NOT NULL,
        result_event_id TEXT,
        success INTEGER,
        error_detected INTEGER DEFAULT 0,
        error_snippet TEXT
    );
CREATE INDEX idx_events_session ON events(session_id, sequence);
CREATE INDEX idx_events_timestamp ON events(timestamp);
CREATE INDEX idx_events_type ON events(event_type);
CREATE INDEX idx_events_tool ON events(tool_name) WHERE tool_name IS NOT NULL;
CREATE INDEX idx_events_file ON events(file_path) WHERE file_path IS NOT NULL;
CREATE INDEX idx_events_tool_use_id ON events(tool_use_id) WHERE tool_use_id IS NOT NULL;
CREATE INDEX idx_projects_last_seen ON projects(last_seen DESC);
CREATE INDEX idx_projects_category ON projects(category);
CREATE INDEX idx_outcomes_outcome ON session_outcomes(outcome);
CREATE INDEX idx_episodes_project ON episodes(project_id);
CREATE INDEX idx_episodes_session ON episodes(session_id);
CREATE INDEX idx_episodes_ended_at ON episodes(ended_at DESC);
CREATE INDEX idx_episodes_status ON episodes(status);
CREATE INDEX idx_tool_pairs_call ON tool_pairs(call_event_id);
CREATE INDEX idx_tool_pairs_error ON tool_pairs(error_detected) WHERE error_detected = 1;
CREATE INDEX idx_git_ops_session ON git_operations(session_id, timestamp);
CREATE INDEX idx_git_ops_hash ON git_operations(commit_hash) WHERE commit_hash IS NOT NULL;
CREATE INDEX idx_git_ops_type ON git_operations(operation_type);
CREATE INDEX idx_segments_session ON conversation_segments(session_id);
CREATE INDEX idx_segments_project ON conversation_segments(project_id);
CREATE INDEX idx_segments_ended_at ON conversation_segments(ended_at DESC);
CREATE INDEX idx_segments_type ON conversation_segments(segment_type);
CREATE VIEW plans_index AS
    SELECT
        session_id,
        event_id,
        file_path,
        timestamp,
        length(new_content) AS bytes
    FROM events
    WHERE file_path LIKE '%/.claude/plans/%.md'
      AND file_operation IN ('write', 'edit', 'multi_edit');
COMMIT;
