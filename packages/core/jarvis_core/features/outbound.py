"""Is this action Jarvis speaking for Arsen to someone else, and did he ask for it?

The e-mail policy (settings.email) rewrites a send to an unapproved recipient into a draft, but it
only sees the mail tools. The browser acts in his own logged-in sites: on 2026-10-19, asked to get
a public file deleted, Jarvis typed a mail to the host's admin in his Gmail, offered to pay a fee
on his behalf, and clicked Send. Which click sends, posts or pays is not something a list of
button labels can know (every site words it differently, in any language), so a model reads the
action in context - his request, the page, what was typed - and answers two questions.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from jarvis_core.models.base import ModelAdapter, ModelTextChunk
from jarvis_proto import Message, Role, ToolCall

_PROMPT = """You check one action an assistant is about to take for {user}, in {user}'s own accounts.

What {user} asked (oldest first; the last line is the current request):
{asked}

What the assistant did just before (tool, arguments, start of the result):
{recent}

The action it is about to take now:
{action}
{target}
Answer two questions.
1. outbound: does this action send, post, publish or submit something to another person or
   organisation in {user}'s name, or pay, offer or promise money or anything else on his behalf?
   Opening, reading, searching, scrolling, filling a field without submitting, or saving a draft
   is not outbound.
2. asked: did {user} explicitly ask for exactly this - this recipient or audience, this content,
   this commitment? A goal ("get it deleted") does not cover a means he never named (mailing a
   stranger), and nothing covers a payment or promise he did not name himself.

Reply with JSON only: {{"outbound": true|false, "asked": true|false, "what": "<one line: what it does, to whom>"}}"""


def _asked(history: list[Message], current: str) -> str:
    said = [m.content.strip() for m in history if m.role is Role.USER and m.name is None and m.content.strip()]
    lines = [s[:400] for s in said[-3:]]
    if not lines or lines[-1] != current.strip()[:400]:
        lines.append(current.strip()[:400])
    return "\n".join(f"- {line}" for line in lines)


def _recent(run_messages: list[Message], limit: int = 6) -> str:
    results = {m.tool_call_id: m.content for m in run_messages if m.role is Role.TOOL and m.tool_call_id}
    steps: list[str] = []
    for m in run_messages:
        for c in m.tool_calls or []:
            args = json.dumps(c.arguments, ensure_ascii=False)[:600]
            head = (results.get(c.id) or "").strip().replace("\n", " ")[:300]
            steps.append(f"- {c.name} {args} -> {head}")
    return "\n".join(steps[-limit:]) or "(nothing yet)"


def _target(call: ToolCall, run_messages: list[Message]) -> str:
    """The line a browser outline gave for the element the call names by @ref, if one did."""
    ref = str(call.arguments.get("ref") or "")
    if not re.fullmatch(r"@?e\d+", ref):
        return ""
    ref = ref if ref.startswith("@") else f"@{ref}"
    for m in reversed(run_messages):
        if m.role is not Role.TOOL:
            continue
        for line in m.content.splitlines():
            if re.search(rf"{re.escape(ref)}\b", line):
                return f"The element {ref} is: {line.strip()[:300]}\n"
    return ""


def _verdict(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) and "outbound" in data and "asked" in data else None


async def unasked_outbound(
    adapter: ModelAdapter,
    call: ToolCall,
    *,
    user: str,
    request: str,
    history: list[Message],
    run_messages: list[Message],
) -> str | None:
    """The reason to stop and ask, or None when the action may go ahead. A checker that fails or
    answers nonsense asks: the cost of one needless question is far below a mail he never meant."""
    prompt = _PROMPT.format(
        user=user,
        asked=_asked(history, request),
        recent=_recent(run_messages),
        action=f"{call.name} {json.dumps(call.arguments, ensure_ascii=False)[:1500]}",
        target=_target(call, run_messages),
    )
    text = ""
    try:
        async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
            if isinstance(chunk, ModelTextChunk):
                text += chunk.text
    except Exception:
        return f"could not check whether {call.name} acts for {user} towards someone else - confirm it"
    data = _verdict(text)
    if data is None:
        return f"could not check whether {call.name} acts for {user} towards someone else - confirm it"
    if data.get("outbound") is True and data.get("asked") is not True:
        what = str(data.get("what") or call.name).strip()[:200]
        return f"acts for {user} towards someone else, and he did not ask for it: {what}"
    return None
