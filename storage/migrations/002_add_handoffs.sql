-- 002_add_handoffs.sql: таблица передач менеджеру

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,                   -- 'booking' | 'complaint' | 'human_requested' | 'no_answer' | 'llm_error' | 'llm_timeout' | 'keyword:<trigger>'
    summary TEXT,                           -- сводка «ЗАПИСЬ: услуга — …, время — …» или NULL
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    notified_at TEXT,                       -- когда уведомили менеджера (первое уведомление)
    reminded_at TEXT,                       -- когда отправлено напоминание (2ч, NULL = не отправлено)
    first_human_reply_at TEXT,              -- первое сообщение от человека (role=human)
    resolved_at TEXT                        -- когда диалог вернулся к боту или закрыт
);

CREATE INDEX IF NOT EXISTS idx_handoffs_conversation_id ON handoffs(conversation_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_created_at ON handoffs(created_at);
CREATE INDEX IF NOT EXISTS idx_handoffs_reason ON handoffs(reason);
CREATE INDEX IF NOT EXISTS idx_handoffs_reminded_at ON handoffs(reminded_at);