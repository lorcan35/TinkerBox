"""NPU Genie LLM backend.

Runs Llama 3.2 1B (or other QAIRT models) on the Qualcomm QCS6490 NPU
via the Genie text-to-text runtime. Achieves ~8 tok/s vs Ollama's ~0.24 tok/s.

The backend spawns genie-t2t-run as a subprocess for each request. The Genie
runtime handles tokenization, HTP graph execution, and detokenization internally.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import AsyncIterator

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend

logger = logging.getLogger(__name__)

# Markers emitted by genie-t2t-run around generated text
_BEGIN_MARKER = "[BEGIN]:"
_END_MARKER = "[END]"
_MAX_RESPONSE_CHARS = 300  # Hard limit — kill genie process after this many chars


class NPUGenieBackend(LLMBackend):
    """LLM backend using Qualcomm Genie runtime on the NPU (HTP)."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._model_dir = Path(config.genie_model_dir)
        self._config_file = config.genie_config or "htp-model-config-llama32-1b-gqa.json"
        self._conversation: list[dict] = []

    async def initialize(self) -> None:
        """Verify genie-t2t-run and model files are present."""
        # Find genie-t2t-run: model dir first, then PATH
        self._genie_bin = self._model_dir / "genie-t2t-run"
        if not self._genie_bin.exists():
            system_bin = shutil.which("genie-t2t-run")
            if system_bin:
                self._genie_bin = Path(system_bin)
            else:
                logger.error(
                    "genie-t2t-run not found in %s or PATH", self._model_dir
                )
                raise FileNotFoundError("genie-t2t-run not found")

        config_path = self._model_dir / self._config_file
        if not config_path.exists():
            logger.error("Genie config not found: %s", config_path)
            raise FileNotFoundError(f"Genie config not found: {config_path}")

        logger.info(
            "NPU Genie backend ready — bin=%s, model_dir=%s, config=%s",
            self._genie_bin,
            self._model_dir,
            self._config_file,
        )

    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        """Run genie-t2t-run and stream the output tokens.

        Genie doesn't support multi-turn natively, so we prepend conversation
        history into the prompt. The system prompt is injected as a preamble.
        """
        sys_prompt = system_prompt or self._config.system_prompt

        # Build a single prompt with history context
        full_prompt = self._build_prompt(prompt, sys_prompt)

        env = os.environ.copy()
        # Ensure libs are findable
        lib_dirs = [
            str(self._model_dir),
            str(self._model_dir.parent / "lib"),
            "/home/radxa/qairt/lib",
        ]
        existing_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = ":".join(lib_dirs) + (":" + existing_ld if existing_ld else "")
        env.setdefault("ADSP_LIBRARY_PATH", str(self._model_dir))

        config_path = str(self._model_dir / self._config_file)

        try:
            proc = await asyncio.create_subprocess_exec(
                str(self._genie_bin),
                "-c", config_path,
                "-p", full_prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._model_dir),
                env=env,
            )
        except OSError as e:
            logger.error("Failed to spawn genie-t2t-run: %s", e)
            yield f"[NPU error: {e}]"
            return

        full_response = []
        in_response = False
        total_chars = 0

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")

            # genie-t2t-run outputs: [PROMPT]: ...\n\n[BEGIN]: tokens...[END]\n
            if _BEGIN_MARKER in line:
                in_response = True
                # Extract text after [BEGIN]:
                text = line.split(_BEGIN_MARKER, 1)[1]
                if _END_MARKER in text:
                    text = text.split(_END_MARKER, 1)[0]
                    in_response = False
                text = text.lstrip()
                if text:
                    full_response.append(text)
                    total_chars += len(text)
                    yield text
                if total_chars >= _MAX_RESPONSE_CHARS:
                    logger.warning("NPU Genie hit %d char limit — killing process", total_chars)
                    proc.kill()
                    break
                continue

            if in_response:
                if _END_MARKER in line:
                    text = line.split(_END_MARKER, 1)[0]
                    in_response = False
                else:
                    text = line
                if text:
                    full_response.append(text)
                    total_chars += len(text)
                    yield text
                if total_chars >= _MAX_RESPONSE_CHARS:
                    logger.warning("NPU Genie hit %d char limit — killing process", total_chars)
                    proc.kill()
                    break

        try:
            await asyncio.wait_for(proc.wait(), timeout=120)
        except asyncio.TimeoutError:
            logger.error("NPU Genie subprocess timed out after 120s — killing")
            proc.kill()
            await proc.wait()

        if proc.returncode != 0:
            stderr = await proc.stderr.read()
            logger.error(
                "genie-t2t-run exited %d: %s",
                proc.returncode,
                stderr.decode("utf-8", errors="replace")[:500],
            )
            if not full_response:
                yield f"[NPU error: exit code {proc.returncode}]"
                return

        # Update conversation history
        response_text = "".join(full_response)
        if response_text:
            self._conversation.append({"role": "user", "content": prompt})
            self._conversation.append({"role": "assistant", "content": response_text})

    def _build_prompt(self, prompt: str, system_prompt: str) -> str:
        """Build a single-shot prompt with history for genie-t2t-run."""
        parts = []
        if system_prompt:
            parts.append(f"System: {system_prompt}\n")
        # Include last few turns of history
        for msg in self._conversation[-6:]:  # Last 3 turns
            role = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{role}: {msg['content']}")
        parts.append(f"User: {prompt}")
        parts.append("Assistant:")
        return "\n".join(parts)

    def clear_history(self) -> None:
        """Clear conversation history."""
        self._conversation.clear()
        logger.debug("NPU Genie conversation history cleared")

    def trim_history(self, max_turns: int = 5) -> None:
        """Keep only the last N turns."""
        max_messages = max_turns * 2
        if len(self._conversation) > max_messages:
            self._conversation = self._conversation[-max_messages:]

    async def shutdown(self) -> None:
        logger.info("NPU Genie backend shut down")

    @property
    def name(self) -> str:
        return f"NPU Genie ({self._config_file})"
