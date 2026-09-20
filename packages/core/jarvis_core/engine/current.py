"""Which run is executing in this task.

The engine sets these once per run task (``run_engine._execute``); everything the run spawns
inherits them, because a task copies its parent's context. Tools are called with their arguments
and nothing else, so this is how a tool learns where it is standing — which conversation it is
speaking from (``sessions.say`` must not write to its own chat) and which run it belongs to (the
chain that stops A → B → A from becoming a conversation between two Jarvises with no one in it).

Outside a run both read as None, which is the honest answer for a REST call or a test.
"""

from __future__ import annotations

from contextvars import ContextVar

current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)
current_conversation_id: ContextVar[str | None] = ContextVar("current_conversation_id", default=None)
