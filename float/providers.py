"""Float — the model layer.

Three providers, one interface. Each one speaks its own dialect of "stream me a
reply and tell me when you want to call a tool", and this module flattens all
three into the same sequence of events::

    {"type": "text",     "text": "..."}          # a token arrived
    {"type": "tool",     "name": ..., "input": {...}, "id": ...}
    {"type": "usage",    "tokens_in": n, "tokens_out": n}
    {"type": "error",    "message": "...", "retryable": bool}
    {"type": "done",     "stop": "end_turn" | "tool_use"}

Transport is ``urllib`` from the standard library, reading the SSE body line by
line. No ``requests``, no vendor SDKs — three SDKs would be 40 MB of dependency
to send the same JSON this file sends in 200 lines, and a school laptop with a
patchy connection should not be waiting on ``pip``.

Errors are translated into something a teacher can act on. "401" is not a
message; "That key was rejected — check it in Settings → Model" is.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

from . import config

USER_AGENT = f"Float/{config.VERSION}"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status


def _friendly(status: int, body: str, provider: str) -> ProviderError:
    label = config.PROVIDERS[provider].label if provider in config.PROVIDERS else provider
    snippet = ""
    try:
        data = json.loads(body)
        snippet = (
            data.get("error", {}).get("message")
            or data.get("message")
            or (data.get("error") if isinstance(data.get("error"), str) else "")
            or ""
        )
    except (ValueError, AttributeError):
        snippet = body[:200]

    if status in (401, 403):
        return ProviderError(
            f"{label} rejected the API key. Open Settings → Model and check it, "
            "or ask your admin to update the school key.",
            status=status,
        )
    if status == 429:
        return ProviderError(
            f"{label} is rate-limiting this key right now. Wait about a minute, or "
            "switch to another provider in the model picker.",
            retryable=True,
            status=status,
        )
    if status == 404:
        return ProviderError(
            f"{label} does not recognise that model. Pick a different one in the model picker.",
            status=status,
        )
    if status in (402, 413):
        return ProviderError(
            f"{label} refused the request: {snippet or 'billing or size limit reached'}.",
            status=status,
        )
    if status >= 500:
        return ProviderError(
            f"{label} had a server problem. Try again in a moment.", retryable=True, status=status
        )
    return ProviderError(snippet or f"{label} returned an error ({status}).", status=status)


def _post_sse(url: str, payload: dict[str, Any], headers: dict[str, str],
              provider: str, timeout: float) -> Iterator[dict[str, Any]]:
    """POST JSON, read an SSE body, yield parsed data objects."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            buffer: list[str] = []
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if buffer:
                        blob = "\n".join(buffer)
                        buffer.clear()
                        if blob.strip() and blob.strip() != "[DONE]":
                            try:
                                yield json.loads(blob)
                            except ValueError:
                                continue
                    continue
                if line.startswith("data:"):
                    buffer.append(line[5:].lstrip())
            if buffer:
                blob = "\n".join(buffer)
                if blob.strip() and blob.strip() != "[DONE]":
                    try:
                        yield json.loads(blob)
                    except ValueError:
                        pass
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise _friendly(exc.code, body, provider) from None
    except urllib.error.URLError as exc:
        raise ProviderError(
            "Could not reach the internet. Check the school's connection and try again.",
            retryable=True,
        ) from exc
    except TimeoutError:
        raise ProviderError("The model took too long to answer. Try a shorter request.",
                            retryable=True) from None


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
def _stream_anthropic(key: str, model: str, system: str, messages: list[dict[str, Any]],
                      tools: list[dict[str, Any]], temperature: float, max_tokens: int,
                      timeout: float) -> Iterator[dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]

    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    blocks: dict[int, dict[str, Any]] = {}
    tokens_in = tokens_out = 0
    stop = "end_turn"

    for event in _post_sse(ANTHROPIC_URL, payload, headers, "anthropic", timeout):
        kind = event.get("type")
        if kind == "message_start":
            tokens_in = event.get("message", {}).get("usage", {}).get("input_tokens", 0)
        elif kind == "content_block_start":
            block = event.get("content_block", {})
            index = event.get("index", 0)
            if block.get("type") == "tool_use":
                blocks[index] = {"id": block.get("id"), "name": block.get("name"), "json": ""}
        elif kind == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                yield {"type": "text", "text": delta.get("text", "")}
            elif delta.get("type") == "input_json_delta":
                index = event.get("index", 0)
                if index in blocks:
                    blocks[index]["json"] += delta.get("partial_json", "")
        elif kind == "content_block_stop":
            index = event.get("index", 0)
            block = blocks.pop(index, None)
            if block:
                yield {
                    "type": "tool",
                    "id": block["id"],
                    "name": block["name"],
                    "input": _loads(block["json"]),
                }
        elif kind == "message_delta":
            tokens_out = event.get("usage", {}).get("output_tokens", tokens_out)
            stop = event.get("delta", {}).get("stop_reason") or stop
        elif kind == "error":
            raise ProviderError(event.get("error", {}).get("message", "Anthropic stream error"))

    yield {"type": "usage", "tokens_in": tokens_in, "tokens_out": tokens_out}
    yield {"type": "done", "stop": "tool_use" if stop == "tool_use" else "end_turn"}


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------
def _stream_openai(key: str, model: str, system: str, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]], temperature: float, max_tokens: int,
                   timeout: float) -> Iterator[dict[str, Any]]:
    chat: list[dict[str, Any]] = []
    if system:
        chat.append({"role": "system", "content": system})
    chat.extend(_to_openai_messages(messages))

    payload: dict[str, Any] = {
        "model": model,
        "messages": chat,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    headers = {"Authorization": f"Bearer {key}"}
    calls: dict[int, dict[str, str]] = {}
    tokens_in = tokens_out = 0
    stop = "end_turn"

    for event in _post_sse(OPENAI_URL, payload, headers, "openai", timeout):
        usage = event.get("usage")
        if usage:
            tokens_in = usage.get("prompt_tokens", tokens_in)
            tokens_out = usage.get("completion_tokens", tokens_out)
        for choice in event.get("choices", []) or []:
            delta = choice.get("delta", {}) or {}
            if delta.get("content"):
                yield {"type": "text", "text": delta["content"]}
            for call in delta.get("tool_calls", []) or []:
                index = call.get("index", 0)
                slot = calls.setdefault(index, {"id": "", "name": "", "json": ""})
                if call.get("id"):
                    slot["id"] = call["id"]
                function = call.get("function", {}) or {}
                if function.get("name"):
                    slot["name"] = function["name"]
                if function.get("arguments"):
                    slot["json"] += function["arguments"]
            if choice.get("finish_reason") == "tool_calls":
                stop = "tool_use"

    for slot in calls.values():
        if slot["name"]:
            yield {"type": "tool", "id": slot["id"] or slot["name"], "name": slot["name"],
                   "input": _loads(slot["json"])}
            stop = "tool_use"

    yield {"type": "usage", "tokens_in": tokens_in, "tokens_out": tokens_out}
    yield {"type": "done", "stop": stop}


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the internal (Anthropic-shaped) history into OpenAI's shape."""
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {"name": block.get("name"),
                                 "arguments": json.dumps(block.get("input", {}))},
                })
            elif btype == "tool_result":
                results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _as_text(block.get("content")),
                })
            elif btype == "image":
                source = block.get("source", {})
                text_parts.append(f"[image: {source.get('media_type', 'attached')}]")

        if role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        elif results:
            out.extend(results)
            if any(p.strip() for p in text_parts):
                out.append({"role": "user", "content": "\n".join(text_parts)})
        else:
            out.append({"role": role, "content": "\n".join(text_parts)})
    return out


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------
def _stream_google(key: str, model: str, system: str, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]], temperature: float, max_tokens: int,
                   timeout: float) -> Iterator[dict[str, Any]]:
    payload: dict[str, Any] = {
        "contents": _to_google_contents(messages),
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        payload["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": _clean_schema(t["parameters"])}
            for t in tools
        ]}]

    url = GOOGLE_URL.format(model=model)
    headers = {"x-goog-api-key": key}
    tokens_in = tokens_out = 0
    stop = "end_turn"
    call_index = 0

    for event in _post_sse(url, payload, headers, "google", timeout):
        meta = event.get("usageMetadata") or {}
        if meta:
            tokens_in = meta.get("promptTokenCount", tokens_in)
            tokens_out = meta.get("candidatesTokenCount", tokens_out)
        for candidate in event.get("candidates", []) or []:
            for part in (candidate.get("content", {}) or {}).get("parts", []) or []:
                if "text" in part and part["text"]:
                    yield {"type": "text", "text": part["text"]}
                function = part.get("functionCall")
                if function:
                    call_index += 1
                    stop = "tool_use"
                    yield {
                        "type": "tool",
                        "id": f"g{call_index}-{function.get('name', 'tool')}",
                        "name": function.get("name", ""),
                        "input": function.get("args", {}) or {},
                    }

    yield {"type": "usage", "tokens_in": tokens_in, "tokens_out": tokens_out}
    yield {"type": "done", "stop": stop}


def _to_google_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        role = "model" if message["role"] == "assistant" else "user"
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": role, "parts": [{"text": content}]})
            continue
        parts: list[dict[str, Any]] = []
        for block in content:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append({"text": block["text"]})
            elif btype == "tool_use":
                parts.append({"functionCall": {"name": block.get("name"),
                                               "args": block.get("input", {})}})
            elif btype == "tool_result":
                parts.append({"functionResponse": {
                    "name": block.get("name") or block.get("tool_use_id", "tool"),
                    "response": {"result": _as_text(block.get("content"))},
                }})
        if parts:
            out.append({"role": role, "parts": parts})
    return out


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Google's function declarations reject a few JSON-Schema keywords."""
    drop = {"additionalProperties", "$schema", "default", "examples", "title"}
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in drop:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _clean_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _clean_schema(value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Helpers and the public entry point
# ---------------------------------------------------------------------------
def _loads(blob: str) -> dict[str, Any]:
    if not blob or not blob.strip():
        return {}
    try:
        value = json.loads(blob)
        return value if isinstance(value, dict) else {"value": value}
    except ValueError:
        return {}


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return json.dumps(content) if content is not None else ""


_STREAMERS = {
    "anthropic": _stream_anthropic,
    "openai": _stream_openai,
    "google": _stream_google,
}


def stream(provider: str, key: str, model: str, system: str,
           messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
           temperature: float = 0.6, max_tokens: int = 4096,
           timeout: float | None = None) -> Iterator[dict[str, Any]]:
    """Stream one turn. Raises ProviderError before yielding anything on a bad setup."""
    timeout = timeout or config.settings.request_timeout
    tools = tools or []

    streamer = _STREAMERS.get(provider)
    if streamer is None:
        # The offline transport, when this machine has it. Import is local and
        # guarded so a missing module is simply a provider that is not there.
        from ._runtime_ext import stream_local, available

        if provider == "local" and available():
            yield from stream_local(model, system, messages, tools, temperature, max_tokens, timeout)
            return
        raise ProviderError(f"Unknown provider '{provider}'.")

    if not key:
        label = config.PROVIDERS[provider].label
        raise ProviderError(
            f"No {label} API key yet. Add one in Settings → Model, or ask your admin "
            "to set the school key."
        )
    yield from streamer(key, model, system, messages, tools, temperature, max_tokens, timeout)


def estimate_paise(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    spec = config.PROVIDERS.get(provider, None)
    model_spec = spec.model(model) if spec else None
    if model_spec is None:
        return 0.0
    return (tokens_in / 1_000_000) * model_spec.cost_in * 100 + \
           (tokens_out / 1_000_000) * model_spec.cost_out * 100


def verify_key(provider: str, key: str) -> tuple[bool, str]:
    """A one-token round trip, so Settings can say 'working' rather than 'saved'."""
    if provider == "local":
        from ._runtime_ext import available

        return (True, "Reachable.") if available() else (False, "Not reachable on this machine.")

    spec = config.PROVIDERS.get(provider)
    if spec is None:
        return False, "Unknown provider."
    try:
        started = time.time()
        for event in stream(provider, key, spec.default_model, "", [{"role": "user", "content": "hi"}],
                            max_tokens=8, timeout=30):
            if event["type"] == "done":
                break
        return True, f"Working. Answered in {time.time() - started:.1f}s."
    except ProviderError as exc:
        return False, exc.message
    except Exception as exc:  # pragma: no cover - network shapes vary
        return False, str(exc)[:200]
