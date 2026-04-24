"""aiohttp middleware for the Dragon voice server.

Each submodule exposes a stateless ``handle_*`` coroutine with the
aiohttp middleware signature ``(request, handler, **deps) -> response``.
The deps that aren't request-scoped (config values, rate-limit bucket
dict, …) are passed in by the caller so each middleware can be tested
without instantiating the full :class:`VoiceServer`.

The ``VoiceServer._*_middleware`` methods remain thin adapters that bind
instance state to these ``handle_*`` functions — so existing test
imports (``srv_mod.VoiceServer._auth_middleware``) keep working.  When
the tests migrate to call the ``handle_*`` functions directly, those
adapter methods can be deleted in a follow-up.
"""

from dragon_voice.middleware import auth, cors, rate_limit, security_headers

__all__ = ["auth", "cors", "rate_limit", "security_headers"]
