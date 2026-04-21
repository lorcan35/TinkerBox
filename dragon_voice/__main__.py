"""Entry point for Dragon Voice Server.

Usage:
    python -m dragon_voice
    python -m dragon_voice --config /path/to/config.yaml
    python -m dragon_voice --stt moonshine --tts kokoro --llm ollama
    python -m dragon_voice --port 3503
"""

import argparse
import logging
import sys

from dragon_voice.config import load_config
from dragon_voice.server import run_server

_BANNER = r"""
  ____                              __     __    _
 |  _ \ _ __ __ _  __ _  ___  _ __ \ \   / /__ (_) ___ ___
 | | | | '__/ _` |/ _` |/ _ \| '_ \ \ \ / / _ \| |/ __/ _ \
 | |_| | | | (_| | (_| | (_) | | | | \ V / (_) | | (_|  __/
 |____/|_|  \__,_|\__, |\___/|_| |_|  \_/ \___/|_|\___\___|
                   |___/
  TinkerClaw Voice Server v0.1.0
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dragon Voice Server — STT/LLM/TTS pipeline for TinkerClaw"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml (default: bundled config or DRAGON_VOICE_CONFIG env var)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override server port (default: 3502)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Override server host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--stt",
        type=str,
        default=None,
        choices=["whisper_cpp", "moonshine", "vosk"],
        help="Override STT backend",
    )
    parser.add_argument(
        "--tts",
        type=str,
        default=None,
        choices=["piper", "kokoro", "edge_tts"],
        help="Override TTS backend",
    )
    parser.add_argument(
        "--llm",
        type=str,
        default=None,
        choices=["ollama", "openrouter", "lmstudio"],
        help="Override LLM backend",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Wave 14 W14-L06: install a defense-in-depth log filter that
    # redacts Bearer tokens and OpenRouter / Anthropic / TinkerClaw
    # API keys from any log line. The wave-13 secret audit found
    # nothing in journals, but any logger.exception(...) on a POST
    # handler that echoes the body would leak the Authorization
    # header; this filter catches that before it lands in journald.
    import re as _re

    class _SecretRedactingFilter(logging.Filter):
        _PATTERNS = (
            (_re.compile(r"Bearer\s+[A-Za-z0-9._\-~+/=]+"), "Bearer <REDACTED>"),
            (_re.compile(r"sk-[A-Za-z0-9\-_]{20,}"), "<REDACTED_API_KEY>"),
            (_re.compile(r"\"api_token\"\s*:\s*\"[^\"]*\""), '"api_token": "<REDACTED>"'),
            (_re.compile(r"\"tinkerclaw_token\"\s*:\s*\"[^\"]*\""),
             '"tinkerclaw_token": "<REDACTED>"'),
        )

        def filter(self, record: logging.LogRecord) -> bool:
            try:
                msg = record.getMessage()
            except Exception:  # noqa: BLE001 — safety net; never raise from filter
                return True
            new_msg = msg
            for pat, replacement in self._PATTERNS:
                new_msg = pat.sub(replacement, new_msg)
            if new_msg != msg:
                record.msg, record.args = new_msg, ()
            return True

    logging.getLogger().addFilter(_SecretRedactingFilter())

    # Load config
    config = load_config(args.config)

    # Apply CLI overrides
    if args.port is not None:
        config.server.port = args.port
    if args.host is not None:
        config.server.host = args.host
    if args.stt is not None:
        config.stt.backend = args.stt
    if args.tts is not None:
        config.tts.backend = args.tts
    if args.llm is not None:
        config.llm.backend = args.llm

    # Print banner
    print(_BANNER)
    print(f"  STT:  {config.stt.backend} (model: {config.stt.model})")
    print(f"  TTS:  {config.tts.backend}")
    print(f"  LLM:  {config.llm.backend}")
    print(f"  Port: {config.server.port}")
    print()

    # Run
    try:
        run_server(config)
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
