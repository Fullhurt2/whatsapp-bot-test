-- 006_add_media_fields.sql: поля для медиа (аудио/фото/видео/документы)

ALTER TABLE messages ADD COLUMN media_path TEXT;
ALTER TABLE messages ADD COLUMN media_mime TEXT;
ALTER TABLE messages ADD COLUMN media_duration_s REAL;
ALTER TABLE messages ADD COLUMN media_size INTEGER;
ALTER TABLE messages ADD COLUMN media_status TEXT; -- 'ok' | 'failed' | 'skipped' | 'expired'
ALTER TABLE messages ADD COLUMN media_model TEXT;
ALTER TABLE messages ADD COLUMN media_cost REAL;

CREATE INDEX IF NOT EXISTS idx_messages_media_status_created ON messages(media_status, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_media_path ON messages(media_path) WHERE media_path IS NOT NULL;
