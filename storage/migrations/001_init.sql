-- 001_init.sql: базовые таблицы conversations, messages, seen_events
-- Применяется первой при инициализации БД

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Диалоги клиентов
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,                    -- UUID
    client_key TEXT NOT NULL,               -- ключ клиента (phone_number_id / accountId / bot_id)
    channel TEXT NOT NULL,                  -- 'zernio' | 'meta' | 'telegram'
    contact_phone TEXT NOT NULL,            -- номер клиента (E.164 для WA, chat_id для TG)
    contact_name TEXT,                      -- имя клиента
    zernio_conversation_id TEXT,            -- conversationId из Zernio (для ответа в тот же диалог)
    status TEXT NOT NULL DEFAULT 'bot',     -- 'bot' | 'manual'
    manual_since TEXT,                      -- когда перешёл в manual (ISO8601)
    last_message_at TEXT,                   -- время последнего сообщения (любого)
    last_client_message_at TEXT,            -- время последнего сообщения от клиента
    unread_count INTEGER NOT NULL DEFAULT 0,-- непрочитанных сообщений для менеджера
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(client_key, contact_phone)
);

CREATE INDEX IF NOT EXISTS idx_conversations_client_key ON conversations(client_key);
CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
CREATE INDEX IF NOT EXISTS idx_conversations_last_message_at ON conversations(last_message_at);
CREATE INDEX IF NOT EXISTS idx_conversations_zernio_conv_id ON conversations(zernio_conversation_id);

-- Сообщения
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                     -- 'client' | 'bot' | 'human'
    text TEXT NOT NULL,
    content_kind TEXT NOT NULL DEFAULT 'text', -- 'text' | 'image' | 'voice' | 'document' | ...
    provider_message_id TEXT,               -- ID сообщения у провайдера (wamid, telegram message_id, Zernio message.id)
    delivery_status TEXT,                   -- 'sending' | 'sent' | 'failed' | 'delivered' | 'read'
    tokens_in INTEGER,                      -- токенов в промпте (для bot)
    tokens_out INTEGER,                     -- токенов в ответе (для bot)
    latency_ms INTEGER,                     -- задержка ответа бота в мс
    answer_kind TEXT,                       -- 'kb' | 'handoff' | 'no_answer' (для bot)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_provider_msg_id ON messages(provider_message_id);

-- Дедупликация вебхуков
CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_seen_events_created_at ON seen_events(created_at);