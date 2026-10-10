"""Admin routers package."""

from admin.routers.auth import register_auth_routes
from admin.routers.clients import register_clients_routes
from admin.routers.chats import register_chats_routes
from admin.routers.analytics import register_analytics_routes
from admin.routers.playground import register_playground_routes

__all__ = [
    "register_auth_routes",
    "register_clients_routes",
    "register_chats_routes",
    "register_analytics_routes",
    "register_playground_routes",
]
