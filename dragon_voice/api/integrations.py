"""REST API routes for integrations (#341 / #342 / #347).

Tab5's Settings → Integrations surface calls these:

  * GET    /api/v1/integrations                          — list + connected accounts
  * POST   /api/v1/integrations/{name}/connect           — start auth flow
  * GET    /api/v1/integrations/{name}/status/{request_id}  — poll flow
  * POST   /api/v1/integrations/{name}/disconnect        — disconnect ALL accounts
  * DELETE /api/v1/integrations/{name}/accounts/{account_id}  — disconnect one (#347)
  * PATCH  /api/v1/integrations/{name}/accounts/{account_id}  — set default (#347)
  * GET    /api/v1/integrations/{name}/test              — smoke test default account
  * GET    /api/v1/integrations/{name}/accounts/{account_id}/test  — per-account smoke test

OAuth callback:
  * GET    /api/v1/oauth/callback  — Google redirect target (public)

All non-callback routes require bearer auth.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Optional

from aiohttp import web

from dragon_voice.api.utils import json_error, parse_json_body
from dragon_voice.tools.integrations import (
    IntegrationBackend,
    IntegrationState,
    list_integrations,
)
from dragon_voice.tools.integrations.oauth import DeviceCodeError
from dragon_voice.tools.integrations.registry import (
    create_integration,
    is_registered,
)

logger = logging.getLogger(__name__)


def _callback_html(message: str, success: bool) -> web.Response:
    """Minimal HTML page rendered after Google's OAuth callback."""
    color = "#0bf08c" if success else "#ff6b6b"
    title = "Connected" if success else "Connection failed"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — TinkerClaw</title>
<style>
  body {{ background:#0a0a0a; color:#e0e0e0; font-family:system-ui;
         display:flex; align-items:center; justify-content:center;
         min-height:100vh; margin:0; padding:32px; box-sizing:border-box; }}
  .card {{ max-width:420px; padding:32px; border-radius:16px;
          background:#141414; border:1px solid #2a2a2a; text-align:center; }}
  h1 {{ color:{color}; margin:0 0 16px; font-size:22px; }}
  p {{ margin:0; line-height:1.5; }}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{message}</p></div></body></html>"""
    return web.Response(text=html, content_type="text/html", charset="utf-8")


class IntegrationRoutes:
    """Registers REST routes + holds one instance per integration so
    in-flight auth flows survive across requests."""

    def __init__(self) -> None:
        self._instances: dict[str, IntegrationBackend] = {}

    def _get(self, name: str) -> IntegrationBackend:
        key = (name or "").strip().lower()
        if not is_registered(key):
            raise KeyError(key)
        if key not in self._instances:
            self._instances[key] = create_integration(key)
        return self._instances[key]

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/integrations", self.list_all)
        app.router.add_post(
            "/api/v1/integrations/{name}/connect", self.connect,
        )
        app.router.add_get(
            "/api/v1/integrations/{name}/status/{request_id}", self.status,
        )
        app.router.add_post(
            "/api/v1/integrations/{name}/disconnect", self.disconnect_all,
        )
        app.router.add_get(
            "/api/v1/integrations/{name}/test", self.test,
        )
        # #347 — per-account routes.
        app.router.add_delete(
            "/api/v1/integrations/{name}/accounts/{account_id}",
            self.disconnect_account,
        )
        app.router.add_patch(
            "/api/v1/integrations/{name}/accounts/{account_id}",
            self.update_account,
        )
        app.router.add_get(
            "/api/v1/integrations/{name}/accounts/{account_id}/test",
            self.test_account,
        )
        # OAuth callback — public (PKCE state authenticates).  See
        # middleware/auth.py PUBLIC_PREFIXES.  Walks every registered
        # integration to find the one holding this state.
        app.router.add_get("/api/v1/oauth/callback", self.oauth_callback)

    # ── handlers ────────────────────────────────────────────────

    async def list_all(self, request: web.Request) -> web.Response:
        del request
        out: list[dict[str, Any]] = []
        for name in list_integrations():
            integ = self._get(name)
            connected = await integ.is_connected()
            accounts = await integ.list_accounts()
            state = (
                IntegrationState.CONNECTED.value
                if connected else IntegrationState.DISCONNECTED.value
            )
            out.append({
                "name": integ.name,
                "display_name": integ.display_name,
                "description": integ.description,
                "auth_kind": integ.auth_kind,
                "state": state,
                "supports_multi_account": integ.supports_multi_account,
                "accounts": [asdict(a) for a in accounts],
            })
        return web.json_response({"integrations": out})

    async def connect(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        body: dict[str, Any] = {}
        if request.content_length and request.content_length > 0:
            body, err = await parse_json_body(request)
            if err:
                return err
        try:
            chal = await integ.start_connect(params=body or None)
        except DeviceCodeError as e:
            status = 503 if e.code == "oauth_not_configured" else 502
            return web.json_response(
                {"error": e.code, "message": e.description},
                status=status,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("integration %s connect failed", name)
            return json_error(f"Connect failed: {e}", 500)
        return web.json_response({
            "kind": chal.kind,
            "request_id": chal.request_id,
            "verification_url": chal.verification_url,
            "user_code": chal.user_code,
            "expires_in_s": chal.expires_in_s,
            "interval_s": chal.interval_s,
            "prompt_fields": chal.prompt_fields,
        })

    async def status(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        request_id = request.match_info["request_id"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        try:
            status = await integ.poll_status(request_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("integration %s poll_status failed", name)
            return json_error(f"poll_status failed: {e}", 500)
        return web.json_response({
            "state": status.state,
            "request_id": status.request_id,
            "error": status.error,
            "account_id": status.account_id,
            "account_label": status.account_label,
        })

    async def disconnect_all(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        try:
            await integ.disconnect()
        except Exception as e:  # noqa: BLE001
            logger.exception("integration %s disconnect-all failed", name)
            return json_error(f"Disconnect failed: {e}", 500)
        return web.json_response({"disconnected": True, "name": name})

    async def disconnect_account(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        account_id = request.match_info["account_id"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        if not await integ.is_connected(account_id):
            return json_error(
                f"Account '{account_id}' is not connected for '{name}'", 404,
            )
        try:
            await integ.disconnect(account_id=account_id)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "integration %s disconnect %s failed", name, account_id,
            )
            return json_error(f"Disconnect failed: {e}", 500)
        return web.json_response({
            "disconnected": True, "name": name, "account_id": account_id,
        })

    async def update_account(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        account_id = request.match_info["account_id"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        body, err = await parse_json_body(request)
        if err:
            return err
        if body.get("default") is True:
            try:
                await integ.set_default_account(account_id)
            except DeviceCodeError as e:
                return web.json_response(
                    {"error": e.code, "message": e.description}, status=404,
                )
            return web.json_response({
                "name": name, "account_id": account_id, "default": True,
            })
        return json_error(
            "PATCH body must contain {\"default\": true} — no other fields supported",
            400,
        )

    async def test(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        ok, detail = await integ.health_check()
        return web.json_response({"ok": ok, "detail": detail, "name": name})

    async def test_account(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        account_id = request.match_info["account_id"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        ok, detail = await integ.health_check(account_id=account_id)
        return web.json_response({
            "ok": ok, "detail": detail, "name": name, "account_id": account_id,
        })

    async def oauth_callback(self, request: web.Request) -> web.Response:
        """Public OAuth redirect target.

        Google sends the user here after consent:
        ``?code=...&state=...`` on success or ``?error=...&state=...``
        on denial.  Walks every instantiated integration looking for
        one that recognizes the state, then hands the code off.
        """
        params = request.query
        state = params.get("state", "")
        code = params.get("code")
        error = params.get("error")

        if not state:
            return _callback_html(
                "Missing state parameter — link may be malformed.", success=False,
            )

        matched: Optional[IntegrationBackend] = None
        for integ in self._instances.values():
            handler = getattr(integ, "handle_callback", None)
            if handler is None:
                continue
            try:
                flow = integ._find_flow_by_state(state)  # type: ignore[attr-defined]
            except AttributeError:
                flow = None
            if flow is not None:
                matched = integ
                break
        if matched is None:
            return _callback_html(
                "This authorization link has already been used or has expired.",
                success=False,
            )

        await matched.handle_callback(state=state, code=code, error=error)  # type: ignore[attr-defined]
        if error:
            return _callback_html(
                f"Google reported an error: {error}.  Go back to the Tab5 "
                f"and try again.",
                success=False,
            )
        return _callback_html(
            "Connected ✓  You can close this tab and return to your Tab5.",
            success=True,
        )
