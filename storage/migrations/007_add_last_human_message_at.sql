-- 007_add_last_human_message_at.sql: поле last_human_message_at в conversations для таймаута manual режима
ALTER TABLE conversations ADD COLUMN last_human_message_at TEXT;
CREATE INDEX IF NOT EXISTS idx_conversations_last_human_msg ON conversations(last_human_message_at);
