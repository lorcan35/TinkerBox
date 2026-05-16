"""Voice-callable tools for Gmail (#341 / Phase 2).

Thin `Tool` wrappers around `GmailIntegration` so the LLM in any vmode
can drive Gmail via the existing ToolRegistry mechanism.

Tools (all accept optional `account` for multi-account routing):
  * ``gmail_unread``  — list unread messages
  * ``gmail_search``  — list messages matching a query
  * ``gmail_read``    — fetch full body of one message
  * ``gmail_send``    — compose + send
  * ``gmail_archive`` — remove INBOX label (== archive)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from dragon_voice.tools.base import Tool
from dragon_voice.tools.integrations.google.gmail import GmailIntegration
from dragon_voice.tools.integrations.oauth import DeviceCodeError

logger = logging.getLogger(__name__)


_shared_instance: Optional[GmailIntegration] = None


def _shared_integration() -> GmailIntegration:
    """Process-wide singleton — same pattern as the Calendar tool."""
    global _shared_instance
    if _shared_instance is None:
        _shared_instance = GmailIntegration()
    return _shared_instance


def _not_connected(action: str) -> dict[str, Any]:
    return {
        "error": "not_connected",
        "message": (
            f"Gmail isn't connected yet, so I can't {action}.  Tap "
            "Settings → Integrations → Connect Gmail on the Tab5."
        ),
    }


_ACCOUNT_ARG_SCHEMA = {
    "type": "string",
    "description": (
        "Optional Gmail account id (typically the email).  Omit to use "
        "the default connected Google account.  Use the user's natural-"
        "language hint to pick — 'my work email' → the work address; "
        "'my personal email' → the personal address."
    ),
}


class GmailUnreadTool(Tool):
    """List unread messages."""

    priority = 30

    @property
    def name(self) -> str:
        return "gmail_unread"

    @property
    def description(self) -> str:
        return (
            "List unread email messages.  Returns the most recent unread "
            "items (from, subject, snippet, id).  Use when the user asks "
            "'do I have any new emails', 'any unread', or 'what's in my "
            "inbox'.  Use `gmail_read` to fetch a specific message's body."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Max messages to return (default 10, max 25).",
                },
                "account": _ACCOUNT_ARG_SCHEMA,
            },
        }

    async def execute(self, args: dict) -> dict:
        integ = _shared_integration()
        account = args.get("account")
        if not await integ.is_connected(account_id=account):
            return _not_connected("read your inbox")
        try:
            messages = await integ.list_messages(
                query="is:unread",
                max_results=int(args.get("max_results", 10)),
                account_id=account,
            )
        except DeviceCodeError as e:
            return {"error": e.code, "message": e.description}
        return {"count": len(messages), "messages": messages, "account": account}


class GmailSearchTool(Tool):
    """Search messages by Gmail query."""

    @property
    def name(self) -> str:
        return "gmail_search"

    @property
    def description(self) -> str:
        return (
            "Search Gmail using Gmail's search syntax (e.g. 'from:alice', "
            "'subject:invoice', 'has:attachment newer_than:7d').  Returns "
            "matching messages with from/subject/snippet/id.  Use `gmail_read` "
            "to fetch a specific message's body."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Gmail search query (Gmail's standard syntax — "
                        "'from:X', 'to:X', 'subject:X', 'has:attachment', "
                        "'newer_than:Nd', etc.)."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max messages to return (default 10, max 25).",
                },
                "account": _ACCOUNT_ARG_SCHEMA,
            },
        }

    async def execute(self, args: dict) -> dict:
        integ = _shared_integration()
        account = args.get("account")
        if not await integ.is_connected(account_id=account):
            return _not_connected("search your inbox")
        try:
            messages = await integ.list_messages(
                query=args["query"],
                max_results=int(args.get("max_results", 10)),
                account_id=account,
            )
        except DeviceCodeError as e:
            return {"error": e.code, "message": e.description}
        return {
            "count": len(messages), "messages": messages,
            "query": args["query"], "account": account,
        }


class GmailReadTool(Tool):
    """Fetch the body of a single message by id."""

    @property
    def name(self) -> str:
        return "gmail_read"

    @property
    def description(self) -> str:
        return (
            "Fetch the full body of a Gmail message by its id.  Use after "
            "`gmail_unread` or `gmail_search` returned a message you want "
            "to read aloud or summarise.  Returns from/subject/date/"
            "body_text (preferred) and body_html (fallback)."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["message_id"],
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message id from a previous gmail_unread/gmail_search.",
                },
                "account": _ACCOUNT_ARG_SCHEMA,
            },
        }

    async def execute(self, args: dict) -> dict:
        integ = _shared_integration()
        account = args.get("account")
        if not await integ.is_connected(account_id=account):
            return _not_connected("read that email")
        try:
            msg = await integ.get_message_body(
                args["message_id"], account_id=account,
            )
        except DeviceCodeError as e:
            return {"error": e.code, "message": e.description}
        return msg


class GmailSendTool(Tool):
    """Compose + send."""

    @property
    def name(self) -> str:
        return "gmail_send"

    @property
    def description(self) -> str:
        return (
            "Send an email through Gmail.  Confirm to/subject/body content "
            "with the user BEFORE calling — sent email is not undoable.  "
            "When replying to a message, pass `in_reply_to` so the reply "
            "threads correctly in the recipient's inbox."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["to", "subject", "body"],
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email (or comma-separated list).",
                },
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body."},
                "in_reply_to": {
                    "type": "string",
                    "description": "Gmail message_id this is a reply to (optional).",
                },
                "cc": {
                    "type": "string",
                    "description": "Optional CC (comma-separated for multiple).",
                },
                "account": _ACCOUNT_ARG_SCHEMA,
            },
        }

    async def execute(self, args: dict) -> dict:
        integ = _shared_integration()
        account = args.get("account")
        if not await integ.is_connected(account_id=account):
            return _not_connected("send an email")
        try:
            return await integ.send_message(
                to=args["to"], subject=args["subject"], body=args["body"],
                in_reply_to=args.get("in_reply_to"), cc=args.get("cc"),
                account_id=account,
            )
        except DeviceCodeError as e:
            return {"error": e.code, "message": e.description}


class GmailArchiveTool(Tool):
    """Archive (remove INBOX label)."""

    @property
    def name(self) -> str:
        return "gmail_archive"

    @property
    def description(self) -> str:
        return (
            "Archive a Gmail message — removes the INBOX label so it stops "
            "showing up in the inbox view.  The message is NOT deleted; "
            "the user can still find it via search.  Confirm message_id "
            "with the user before calling."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["message_id"],
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message id from a previous gmail_unread/gmail_search.",
                },
                "account": _ACCOUNT_ARG_SCHEMA,
            },
        }

    async def execute(self, args: dict) -> dict:
        integ = _shared_integration()
        account = args.get("account")
        if not await integ.is_connected(account_id=account):
            return _not_connected("archive that email")
        try:
            return await integ.modify_labels(
                args["message_id"], remove=["INBOX"], account_id=account,
            )
        except DeviceCodeError as e:
            return {"error": e.code, "message": e.description}
