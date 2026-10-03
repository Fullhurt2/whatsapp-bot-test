-- 004_add_unanswered.sql: вопросы без ответа и их группировка

CREATE TABLE IF NOT EXISTS unanswered_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    question TEXT NOT NULL,                 -- исходный вопрос клиента
    status TEXT NOT NULL DEFAULT 'new',     -- 'new' | 'answered' | 'ignored'
    group_id INTEGER,                       -- FK на unanswered_groups (NULL = не сгруппирован)
    answer_text TEXT,                       -- ответ, который добавили в базу знаний
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_unanswered_client_key ON unanswered_questions(client_key);
CREATE INDEX IF NOT EXISTS idx_unanswered_status ON unanswered_questions(status);
CREATE INDEX IF NOT EXISTS idx_unanswered_group_id ON unanswered_questions(group_id);
CREATE INDEX IF NOT EXISTS idx_unanswered_created_at ON unanswered_questions(created_at);

-- Группы похожих вопросов (создаются LLM)
CREATE TABLE IF NOT EXISTS unanswered_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key TEXT NOT NULL,
    name TEXT NOT NULL,                     -- название группы (сформулировано LLM)
    question_ids TEXT NOT NULL,             -- JSON массив id вопросов: [1, 5, 12]
    status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'answered' | 'ignored'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_unanswered_groups_client_key ON unanswered_groups(client_key);
CREATE INDEX IF NOT EXISTS idx_unanswered_groups_status ON unanswered_groups(status);