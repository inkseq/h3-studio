"""Server-sent events for one story chat turn (#1083).

The turn itself is unchanged: the model's JSON plan is applied only when it is
complete, in the same code path as the plain POST. While the model writes, the
stream carries the reply text seen so far, then the stored result.

Events (each `data:` is one JSON object):
  progress  {"step": "thinking" | "applying"}
  reply     {"text": <reply so far>}
  done      <the same body the plain POST returns>
  error     {"status": <http status>, "detail": <the same error detail>}
"""

from __future__ import annotations

import json
import queue
import threading

from fastapi import HTTPException

HEARTBEAT_SECONDS = 15
_END = object()


def sse(event, data):
    return (
        "event: "
        + event
        + "\ndata: "
        + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


def reply_so_far(partial):
    """The `reply` string of a JSON object that is still being written.

    The prompt puts `reply` first. Returns None until its opening quote has
    arrived; never returns half of an escape sequence.
    """
    marker = partial.find('"reply"')
    if marker < 0:
        return None
    index = partial.find(":", marker + 7)
    if index < 0:
        return None
    index += 1
    while index < len(partial) and partial[index] in " \t\r\n":
        index += 1
    if index >= len(partial) or partial[index] != '"':
        return None
    out, index = [], index + 1
    escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n"}
    escapes.update(r="\r", t="\t")
    while index < len(partial):
        char = partial[index]
        if char == '"':
            break
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(partial):
            break
        code = partial[index + 1]
        if code == "u":
            digits = partial[index + 2 : index + 6]
            if len(digits) < 4:
                break
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                break
            index += 6
            continue
        out.append(escapes.get(code, code))
        index += 2
    return "".join(out)


def call_model(client, call, options, progress=None):
    """generate(), or generate_stream() reporting the reply as it arrives."""
    if progress is None:
        return client.generate(*call, **options)
    progress("progress", step="thinking")
    if not hasattr(client, "generate_stream"):
        return client.generate(*call, **options)

    def seen(partial):
        reply = reply_so_far(partial)
        if reply:
            progress("reply", text=reply)

    return client.generate_stream(*call, **options, on_text=seen)


def stream_turn(run, *, heartbeat=HEARTBEAT_SECONDS):
    """Run `run(progress)` in a worker and yield its events as SSE text.

    `progress(event, **data)` may be called from the worker. The worker's
    return value becomes `done`; an HTTPException becomes `error` with the same
    detail the plain POST would return.
    """
    events = queue.Queue()

    def progress(event, **data):
        events.put(sse(event, data))

    def work():
        try:
            events.put(sse("done", run(progress)))
        except HTTPException as error:
            events.put(
                sse("error", {"status": error.status_code, "detail": error.detail})
            )
        except Exception:
            events.put(
                sse("error", {"status": 500, "detail": {"code": "story_chat_failed"}})
            )
        finally:
            events.put(_END)

    threading.Thread(target=work, daemon=True).start()
    last_reply = None
    while True:
        try:
            item = events.get(timeout=heartbeat)
        except queue.Empty:
            yield ": keep-alive\n\n"
            continue
        if item is _END:
            return
        if item.startswith("event: reply\n"):
            # Drop repeats so a long thinking phase does not resend the same text.
            if item == last_reply:
                continue
            last_reply = item
        yield item
