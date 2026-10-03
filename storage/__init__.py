"""storage/__init__.py — единый экспорт пакета хранилища."""

from storage.db import (
    get_db_path,
    get_connection,
    close_connection,
    transaction,
    execute,
    executemany,
    fetchone,
    fetchall,
    init_db,
    vacuum,
    backup,
)

from storage.schema import (
    apply_migrations,
    get_current_version,
    create_schema_version_table,
)

from storage.conversations import (
    create_conversation,
    get_conversation,
    get_conversation_by_client_and_phone,
    list_conversations,
    update_conversation_status,
    increment_unread,
    mark_read,
    update_last_message_times,
    get_conversations_needing_timeout_check,
    delete_conversation,
    cleanup_old_conversations,
)

from storage.messages import (
    add_message,
    get_message,
    get_messages,
    get_context_for_llm,
    get_last_client_message_at,
    update_delivery_status,
    get_message_by_provider_id,
    count_messages_since,
    get_bot_messages_stats,
    get_conversation_stats,
    get_hourly_distribution,
    cleanup_old_seen_events,
)

from storage.seen_events import (
    check_and_add,
    is_seen,
    add_event,
    cleanup_old,
    get_count,
)

from storage.handoffs import (
    create_handoff,
    get_handoff,
    get_handoffs_for_conversation,
    get_open_handoff,
    mark_notified,
    mark_first_human_reply,
    resolve_handoff,
    get_handoff_stats,
    get_manager_response_times,
)

from storage.unanswered import (
    add_unanswered_question,
    get_unanswered_question,
    list_unanswered_questions,
    mark_question_answered,
    mark_question_ignored,
    create_unanswered_group,
    get_unanswered_group,
    list_unanswered_groups,
    update_group_status,
    get_questions_for_group,
    get_unanswered_stats,
    get_recent_unanswered_for_regroup,
    get_last_regroup_time,
)

from storage.tg_bindings import (
    generate_link_code,
    create_link_code,
    verify_link_code,
    complete_link_code,
    add_tg_binding,
    get_tg_bindings,
    remove_tg_binding,
    get_tg_bindings_for_notify,
    cleanup_expired_link_codes,
)

from storage.stats import (
    get_client_stats,
    get_admin_overview_stats,
    export_stats_csv,
    parse_date_range,
)

__all__ = [
    # db
    "get_db_path",
    "get_connection",
    "close_connection",
    "transaction",
    "execute",
    "executemany",
    "fetchone",
    "fetchall",
    "init_db",
    "vacuum",
    "backup",
    # schema
    "apply_migrations",
    "get_current_version",
    "create_schema_version_table",
    # conversations
    "create_conversation",
    "get_conversation",
    "get_conversation_by_client_and_phone",
    "list_conversations",
    "update_conversation_status",
    "increment_unread",
    "mark_read",
    "update_last_message_times",
    "get_conversations_needing_timeout_check",
    "delete_conversation",
    "cleanup_old_conversations",
    # messages
    "add_message",
    "get_message",
    "get_messages",
    "get_context_for_llm",
    "get_last_client_message_at",
    "update_delivery_status",
    "get_message_by_provider_id",
    "count_messages_since",
    "get_bot_messages_stats",
    "get_conversation_stats",
    "get_hourly_distribution",
    "cleanup_old_seen_events",
    # seen_events
    "check_and_add",
    "is_seen",
    "add_event",
    "cleanup_old",
    "get_count",
    # handoffs
    "create_handoff",
    "get_handoff",
    "get_handoffs_for_conversation",
    "get_open_handoff",
    "mark_notified",
    "mark_first_human_reply",
    "resolve_handoff",
    "get_handoffs_stats",
    "get_manager_response_times",
    # unanswered
    "add_unanswered_question",
    "get_unanswered_question",
    "list_unanswered_questions",
    "mark_question_answered",
    "mark_question_ignored",
    "create_unanswered_group",
    "get_unanswered_group",
    "list_unanswered_groups",
    "update_group_status",
    "get_questions_for_group",
    "get_unanswered_stats",
    "get_recent_unanswered_for_regroup",
    "get_last_regroup_time",
    # tg_bindings
    "generate_link_code",
    "create_link_code",
    "verify_link_code",
    "complete_link_code",
    "add_tg_binding",
    "get_tg_bindings",
    "remove_tg_binding",
    "get_tg_bindings_for_notify",
    "cleanup_expired_link_codes",
    # stats
    "get_client_stats",
    "get_admin_overview_stats",
    "export_stats_csv",
    "parse_date_range",
]