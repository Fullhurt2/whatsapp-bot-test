-- 005_add_stats_indexes.sql: дополнительные индексы для аналитических запросов

-- Составной индекс для выборки сообщений клиента за период
CREATE INDEX IF NOT EXISTS idx_messages_conv_role_created
    ON messages(conversation_id, role, created_at);

-- Для подсчёта диалогов за период
CREATE INDEX IF NOT EXISTS idx_conversations_client_created
    ON conversations(client_key, created_at);

-- Для времени ответа бота (пары client -> bot)
CREATE INDEX IF NOT EXISTS idx_messages_role_created_latency
    ON messages(role, created_at) WHERE role = 'bot' AND latency_ms IS NOT NULL;

-- Для handoffs по причинам за период
CREATE INDEX IF NOT EXISTS idx_handoffs_reason_created
    ON handoffs(reason, created_at);

-- Для unanswered questions
CREATE INDEX IF NOT EXISTS idx_unanswered_client_status_created
    ON unanswered_questions(client_key, status, created_at);

-- Для тестовых номеров (исключение из статистики) - будет использоваться в коде
-- Индекс не нужен, фильтрация в Python