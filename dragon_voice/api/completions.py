"""Direct LLM completion API (stateless, no session)."""

import json
import logging
import time

from aiohttp import web

from dragon_voice.api.utils import json_error, parse_json_body
from dragon_voice.conversation import ConversationEngine

logger = logging.getLogger(__name__)


class CompletionRoutes:
    def __init__(self, conversation: ConversationEngine | None = None) -> None:
        self._conversation = conversation

    def register(self, app: web.Application) -> None:
        app.router.add_post("/api/v1/completions", self.completions)

    async def completions(self, request: web.Request) -> web.Response:
        """POST /api/v1/completions — stateless LLM completion

        Request: {"messages": [...], "stream": true, "max_tokens": 128, "temperature": 0.7}
        Response (stream=true): SSE with {"token": "..."} then [DONE]
        Response (stream=false): {"content": "...", "model": "...", "latency_ms": 123}
        """
        if not self._conversation or not self._conversation.llm:
            return json_error("LLM backend not available", 503)

        body, err = await parse_json_body(request)
        if err:
            return err

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return json_error("'messages' field is required (list of {role, content})")

        stream = body.get("stream", True)
        llm = self._conversation.llm

        if stream:
            response = web.StreamResponse(headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            })
            await response.prepare(request)

            try:
                t0 = time.monotonic()
                async for token in llm.generate_stream_with_messages(messages):
                    data = json.dumps({"token": token})
                    await response.write(f"data: {data}\n\n".encode())
                latency_ms = (time.monotonic() - t0) * 1000
                await response.write(f"data: {json.dumps({'latency_ms': round(latency_ms), 'model': llm.name})}\n\n".encode())
            except Exception as e:
                logger.exception("Completion error")
                await response.write(f"data: {json.dumps({'error': str(e)})}\n\n".encode())

            await response.write(b"data: [DONE]\n\n")
            return response
        else:
            # Non-streaming
            try:
                t0 = time.monotonic()
                full_response = []
                async for token in llm.generate_stream_with_messages(messages):
                    full_response.append(token)
                latency_ms = (time.monotonic() - t0) * 1000
                content = "".join(full_response)
                return web.json_response({
                    "content": content,
                    "model": llm.name,
                    "latency_ms": round(latency_ms),
                    "token_count": len(full_response),
                })
            except Exception as e:
                logger.exception("Completion error")
                return json_error(f"Completion failed: {e}", 500)
