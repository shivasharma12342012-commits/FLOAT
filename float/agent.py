"""Float — the turn loop.

One function does the work: ``run`` takes a conversation and yields events for
the browser. Between the model and the teacher it handles the tool round trip,
retries the one failure worth retrying, keeps the transcript in the database, and
records what the turn cost.

Events yielded (each is sent to the browser as one SSE frame)::

    {"type": "start",    "conv_id": n, "title": "..."}
    {"type": "text",     "text": "..."}
    {"type": "tool",     "name": "...", "label": "..."}
    {"type": "card",     "card": {...}}          # a file, a task, a run of steps
    {"type": "step",     "text": "..."}          # a pointer action, live
    {"type": "error",    "message": "..."}
    {"type": "done",     "usage": {...}}
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Iterator

from . import config, db, providers, tools

MAX_TOOL_ROUNDS = 6
TOOL_LABELS = {
    "create_file": "Making the file",
    "look_up_class": "Looking up the class",
    "save_task": "Adding to your list",
    "use_computer": "Using your computer",
    "read_my_day": "Checking your day",
}


def _history(conv_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """Rebuild the model-facing history from stored messages."""
    rows = db.get_messages(conv_id)[-limit:]
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["role"] not in {"user", "assistant"}:
            continue
        blocks = row["meta"].get("blocks")
        out.append({"role": row["role"], "content": blocks if blocks else row["content"]})
    return out


def title_for(text: str) -> str:
    clean = " ".join(text.strip().split())
    if len(clean) <= 52:
        return clean or "New conversation"
    cut = clean[:52].rsplit(" ", 1)[0]
    return (cut or clean[:52]) + "…"


def pick_model(user: dict[str, Any], requested: str = "") -> tuple[str, str, str]:
    """Return (provider, model, key). Falls back to whatever the school has set up."""
    allowed = db.allowed_providers()
    provider, _, model = (requested or "").partition(":")

    if provider == "local" and config.rt_enabled():
        return "local", model or config.RT_MODEL, ""

    if provider in allowed:
        key = db.get_key(provider, int(user["id"]))
        if key:
            spec = config.PROVIDERS[provider]
            return provider, model or spec.default_model, key

    for candidate in allowed:
        key = db.get_key(candidate, int(user["id"]))
        if key:
            return candidate, config.PROVIDERS[candidate].default_model, key

    if config.rt_enabled():
        return "local", config.RT_MODEL, ""

    return (provider or "anthropic"), model, ""


def run(user: dict[str, Any], conv_id: int, message: str, model_choice: str = "",
        allow_computer: bool = True, attachments: list[dict[str, Any]] | None = None
        ) -> Iterator[dict[str, Any]]:
    """Take one turn. Yields events; never raises for an expected failure."""
    started = time.time()
    provider, model, key = pick_model(user, model_choice)

    conversation = db.get_conversation(conv_id, int(user["id"]))
    if conversation is None:
        yield {"type": "error", "message": "That conversation is gone. Start a new one."}
        return

    user_blocks: list[dict[str, Any]] = []
    if message.strip():
        user_blocks.append({"type": "text", "text": message})
    for attachment in attachments or []:
        if attachment.get("kind") == "image" and provider != "local":
            user_blocks.append({
                "type": "image",
                "source": {"type": "base64",
                           "media_type": attachment.get("media_type", "image/png"),
                           "data": attachment.get("data", "")},
            })
        elif attachment.get("text"):
            user_blocks.append({
                "type": "text",
                "text": f"\n\n--- {attachment.get('name', 'attached file')} ---\n"
                        f"{attachment['text'][:120_000]}",
            })

    db.add_message(conv_id, "user", message, {"blocks": user_blocks if len(user_blocks) > 1 else None})

    if conversation["title"] in {"New conversation", ""} and message.strip():
        new_title = title_for(message)
        db.rename_conversation(conv_id, int(user["id"]), new_title)
        yield {"type": "start", "conv_id": conv_id, "title": new_title}
    else:
        yield {"type": "start", "conv_id": conv_id, "title": conversation["title"]}

    history = _history(conv_id)
    system = tools.system_prompt(user)
    schemas = tools.tools_for(user, allow_computer and config.settings.computer_control)

    total_in = total_out = 0
    reply_text: list[str] = []

    for round_index in range(MAX_TOOL_ROUNDS):
        assistant_blocks: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        buffer: list[str] = []
        stop = "end_turn"

        try:
            for event in _with_retry(provider, key, model, system, history, schemas):
                kind = event["type"]
                if kind == "text":
                    buffer.append(event["text"])
                    reply_text.append(event["text"])
                    yield {"type": "text", "text": event["text"]}
                elif kind == "tool":
                    if buffer:
                        assistant_blocks.append({"type": "text", "text": "".join(buffer)})
                        buffer.clear()
                    assistant_blocks.append({
                        "type": "tool_use", "id": event["id"],
                        "name": event["name"], "input": event["input"],
                    })
                    pending.append(event)
                    yield {"type": "tool", "name": event["name"],
                           "label": TOOL_LABELS.get(event["name"], "Working")}
                elif kind == "usage":
                    total_in += event.get("tokens_in", 0)
                    total_out += event.get("tokens_out", 0)
                elif kind == "done":
                    stop = event.get("stop", "end_turn")
        except providers.ProviderError as exc:
            db.record_usage(int(user["id"]), provider, model, total_in, total_out, 0,
                            int((time.time() - started) * 1000), ok=False)
            db.audit("chat.error", user_id=int(user["id"]), actor=user.get("name", ""),
                     detail=exc.message[:300], level="error")
            yield {"type": "error", "message": exc.message}
            return

        if buffer:
            assistant_blocks.append({"type": "text", "text": "".join(buffer)})

        if not pending or stop != "tool_use":
            db.add_message(conv_id, "assistant", "".join(reply_text),
                           {"blocks": assistant_blocks if len(assistant_blocks) > 1 else None,
                            "provider": provider, "model": model})
            break

        history.append({"role": "assistant", "content": assistant_blocks})
        db.add_message(conv_id, "assistant", "".join(buffer), {"blocks": assistant_blocks,
                                                               "provider": provider, "model": model})

        results: list[dict[str, Any]] = []
        step_queue: queue.Queue = queue.Queue()

        for call in pending:
            def emit(payload: dict[str, Any]) -> None:
                step_queue.put(payload)

            outcome = _call_tool(call, user, conv_id, emit, step_queue)
            for item in _drain(step_queue):
                yield item
            if outcome.get("card"):
                yield {"type": "card", "card": outcome["card"]}
            results.append({
                "type": "tool_result",
                "tool_use_id": call["id"],
                "name": call["name"],
                "content": outcome["text"][:20_000],
            })

        history.append({"role": "user", "content": results})
        db.add_message(conv_id, "tool", json.dumps([r["name"] for r in results]),
                       {"blocks": results})
    else:
        yield {"type": "error",
               "message": "That took more tool steps than Float allows in one go. "
                          "Ask for a smaller piece of it."}

    paise = providers.estimate_paise(provider, model, total_in, total_out)
    db.record_usage(int(user["id"]), provider, model, total_in, total_out, paise,
                    int((time.time() - started) * 1000), ok=True)

    yield {
        "type": "done",
        "usage": {
            "tokens_in": total_in,
            "tokens_out": total_out,
            "paise": round(paise, 2),
            "seconds": round(time.time() - started, 1),
            "provider": provider,
            "model": model,
        },
    }


def _drain(step_queue: "queue.Queue") -> list[dict[str, Any]]:
    out = []
    while True:
        try:
            out.append(step_queue.get_nowait())
        except queue.Empty:
            return out


def _call_tool(call: dict[str, Any], user: dict[str, Any], conv_id: int,
               emit: Any, step_queue: "queue.Queue") -> dict[str, Any]:
    """Run a tool on a worker thread so a long pointer run still streams steps."""
    result: dict[str, Any] = {}

    def work() -> None:
        result.update(tools.execute(call["name"], call.get("input", {}), user, conv_id, emit))

    thread = threading.Thread(target=work, daemon=True, name=f"float-tool-{call['name']}")
    thread.start()
    thread.join(timeout=600)
    if thread.is_alive():
        return {"text": "That tool ran too long and was abandoned.", "card": None}
    return result or {"text": "The tool returned nothing.", "card": None}


def _with_retry(provider: str, key: str, model: str, system: str,
                history: list[dict[str, Any]], schemas: list[dict[str, Any]]
                ) -> Iterator[dict[str, Any]]:
    """One retry, and only for the failures where retrying is the right answer.

    Retrying a rejected key just rejects it again more slowly, so that one goes
    straight back to the teacher.
    """
    attempt = 0
    while True:
        try:
            yield from providers.stream(provider, key, model, system, history, schemas)
            return
        except providers.ProviderError as exc:
            if not exc.retryable or attempt >= 1:
                raise
            attempt += 1
            time.sleep(1.5)
