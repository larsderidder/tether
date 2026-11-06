#!/usr/bin/env python3
"""Deterministic Pi RPC peer for process-level tests, without any model calls."""

import json
import os
import sys


def emit(event: dict) -> None:
    """Write one complete RPC frame and flush the subprocess pipe."""
    print(json.dumps(event), flush=True)


def answer(message: str) -> None:
    """Return streamed text followed by Pi's final assistant message."""
    text = f"answer: {message}"
    emit({"type": "agent_start"})
    emit(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": text},
        }
    )
    emit(
        {
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }
    )


def main() -> None:
    """Read the real runner's commands and model success, abort and crash paths."""
    ignore_abort = False
    for line in sys.stdin:
        command = json.loads(line)
        kind = command.get("type")
        if kind == "get_state":
            emit(
                {
                    "type": "response",
                    "command": "get_state",
                    "success": True,
                    "data": {
                        "sessionId": f"fake-{os.getpid()}",
                        "model": {"id": "fake", "provider": "test"},
                    },
                }
            )
        elif kind in ("prompt", "follow_up"):
            message = command["message"]
            if message == "ignore-abort":
                ignore_abort = True
                emit({"type": "agent_start"})
                continue
            if message == "crash":
                print("deterministic fake process failure", file=sys.stderr, flush=True)
                sys.exit(2)
            if message == "hang":
                emit({"type": "agent_start"})
                continue
            if message == "noisy":
                sys.stderr.write("diagnostic output\n" * 20000)
                sys.stderr.flush()
            answer(message)
        elif kind == "abort":
            if ignore_abort:
                continue
            emit({"type": "agent_end", "messages": []})
            return


if __name__ == "__main__":
    main()
