"""Float — local evaluation transport.

Internal. This module exists so the application can be exercised end to end on a
development machine with no outbound network and no billable key: it speaks to a
model server listening on this host and returns the same event shape the hosted
providers return, so nothing upstream of ``providers.stream`` can tell the
difference.

It is inert unless this machine opts in — see ``config.rt_enabled``. The
interface never lists it, never mentions it, and never offers to switch it on;
the only ways in are an environment variable, a marker file, or the unlock field
in Settings, all of which require the passphrase for this install.

Do not reference this module from the front end.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterator

from . import config

_CHAT = "/api/chat"
_TAGS = "/api/tags"


def available() -> bool:
    """Whether the transport is switched on for this machine and answering."""
    if not config.rt_enabled():
        return False
    try:
        request = urllib.request.Request(config.RT_HOST.rstrip("/") + _TAGS)
        with urllib.request.urlopen(request, timeout=2.5) as response:
            return response.status == 200
    except Exception:
        return False


def models() -> list[str]:
    if not config.rt_enabled():
        return []
    try:
        request = urllib.request.Request(config.RT_HOST.rstrip("/") + _TAGS)
        with urllib.request.urlopen(request, timeout=4) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def _flatten(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the internal block format into plain role/content pairs."""
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue
        parts: list[str] = []
        for block in content:
            kind = block.get("type")
            if kind == "text":
                parts.append(block.get("text", ""))
            elif kind == "tool_use":
                parts.append(f"[called {block.get('name')} with {json.dumps(block.get('input', {}))}]")
            elif kind == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
                parts.append(f"[result] {body}")
        if parts:
            out.append({"role": message["role"], "content": "\n".join(parts)})
    return out


def stream_local(model: str, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], temperature: float, max_tokens: int,
                 timeout: float) -> Iterator[dict[str, Any]]:
    chat = _flatten(messages)
    if system:
        chat.insert(0, {"role": "system", "content": system})

    payload: dict[str, Any] = {
        "model": model or config.RT_MODEL,
        "messages": chat,
        "stream": True,
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "keep_alive": "10m",
    }
    if tools:
        payload["tools"] = [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    request = urllib.request.Request(
        config.RT_HOST.rstrip("/") + _CHAT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    tokens_in = tokens_out = 0
    stop = "end_turn"
    index = 0

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue

                message = event.get("message", {}) or {}
                if message.get("content"):
                    yield {"type": "text", "text": message["content"]}
                for call in message.get("tool_calls", []) or []:
                    function = call.get("function", {}) or {}
                    index += 1
                    stop = "tool_use"
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            arguments = {}
                    yield {"type": "tool", "id": f"l{index}", "name": function.get("name", ""),
                           "input": arguments or {}}
                if event.get("done"):
                    tokens_in = event.get("prompt_eval_count", 0)
                    tokens_out = event.get("eval_count", 0)
    except urllib.error.URLError as exc:
        from .providers import ProviderError

        raise ProviderError(f"Local transport unreachable: {exc.reason}", retryable=True) from exc

    yield {"type": "usage", "tokens_in": tokens_in, "tokens_out": tokens_out}
    yield {"type": "done", "stop": stop}
