"""Runs LLM agents and, if switched on, records every run in trace files for review.

All agent calls go through `run_agent`, so the trace shows the whole conversation of a run in
order: for each call, the agent, its model, the instructions it was given (filled-in prompt
from prompts/), the exact input the orchestrator sent it, and its structured reply (or the
error). Agents never call each other: the orchestrator code builds every input from earlier
outputs, so the trace is everything any agent saw or said.

    AGENT_TRACE=ON    writes two files per process (created at the first event; a running
                      server appends to one pair). Default: OFF
        logs/trace-<timestamp>.md      readable in an editor: one section per agent call
        logs/trace-<timestamp>.jsonl   one JSON event per line, for the /runs page
                                       (api.py, static/runs.html)

Events in the .jsonl file:
    operation_start / operation_end   one user action (process, draft, rank, job create, ...),
                                      from `traced_operation` / `trace_operation`; nested
                                      actions (upload -> process) count as the outer one
    agent                             an agent call: agent, model, start, seconds, ok,
                                      instructions, input, output or error
    step                              a plain-code step between agent calls (`trace_step`):
                                      stage, name and a small summary (names, counts,
                                      pseudonymous refs; never candidate values or real IDs)
Every event carries the id of the operation it belongs to (None outside one).

Privacy: the trace holds the same payloads as DEBUG logging. Rankers only ever receive
pseudonymized candidates and the other agents receive no candidate rows, but the payloads
still hold applicant details. The ref -> real-ID map is never written. logs/ is gitignored;
don't share these files.

The OpenAI Agents SDK's own tracing (which uploads traces to the OpenAI dashboard) is turned
off in llm_agents/__init__.py; these files stay on this machine.
"""

import functools
import itertools
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from agents import Agent, Runner
from pydantic import BaseModel

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
TRACE_NAME = re.compile(r"^trace-\d{8}-\d{6}\.jsonl$")

T = TypeVar("T", bound=BaseModel)

_lock = threading.Lock()
_path: Path | None = None          # the .md file; the .jsonl file sits next to it
_calls = 0
_op_ids = itertools.count(1)
_current_op: ContextVar[str | None] = ContextVar("trace_operation", default=None)


def trace_enabled() -> bool:
    value = os.getenv("AGENT_TRACE", "OFF").strip().upper()
    if value not in {"ON", "OFF"}:
        raise ValueError(f"AGENT_TRACE={value!r} is invalid; use ON or OFF")
    return value == "ON"


def trace_path() -> Path | None:
    """The current .md trace file, or None if nothing has been traced yet."""
    return _path


def _ensure_files(log_dir: Path | None) -> Path:
    """Create this process's trace files on first use. Call with _lock held."""
    global _path
    if _path is None:
        log_dir = log_dir or LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        _path = log_dir / f"trace-{datetime.now():%Y%m%d-%H%M%S}.md"
        _path.write_text(f"# Agent trace, {datetime.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")
    return _path


def _emit(event: dict[str, Any], md: str = "", log_dir: Path | None = None) -> None:
    """Append one event to the .jsonl file (and `md` to the .md file)."""
    event = {"op": _current_op.get(), "time": time.time(), **event}
    line = json.dumps(event, ensure_ascii=False, default=str)
    with _lock:
        path = _ensure_files(log_dir)
        with path.with_suffix(".jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if md:
            with path.open("a", encoding="utf-8") as f:
                f.write(md)


def _as_data(text: str) -> Any:
    """JSON text as data, anything else as is."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _pretty(text: str) -> tuple[str, str]:
    """(fence language, text): JSON input is re-indented, anything else shown as is."""
    try:
        return "json", json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return "text", text


# --- Operations and code steps ---

@contextmanager
def trace_operation(name: str, detail: str = "", log_dir: Path | None = None):
    """Group everything inside into one operation (a user action). No-op when tracing is off
    or another operation is already open (the outer one wins)."""
    if not trace_enabled() or _current_op.get() is not None:
        yield
        return
    op = f"op-{next(_op_ids)}"
    token = _current_op.set(op)
    start = time.time()
    _emit({"type": "operation_start", "name": name, "detail": detail[:200]},
          f"\n---\n\n# Operation: {name}{f' ({detail[:200]})' if detail else ''}\n", log_dir)
    try:
        yield
    except BaseException as e:
        _emit({"type": "operation_end", "seconds": round(time.time() - start, 3), "ok": False,
               "error": f"{type(e).__name__}: {e}"}, log_dir=log_dir)
        raise
    else:
        _emit({"type": "operation_end", "seconds": round(time.time() - start, 3), "ok": True}, log_dir=log_dir)
    finally:
        _current_op.reset(token)


def traced_operation(name: str, detail: Callable[..., str] | None = None):
    """Decorator for an async entry point: runs it inside `trace_operation(name, detail(...))`.
    `detail` gets the same arguments as the function and returns a short description."""
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            text = ""
            if detail is not None and trace_enabled():
                try:
                    text = str(detail(*args, **kwargs))
                except Exception:   # a description must never break the call
                    text = ""
            with trace_operation(name, text):
                return await fn(*args, **kwargs)
        return wrapper
    return decorate


def trace_step(stage: str, name: str, **details: Any) -> None:
    """Record a plain-code step. `details` must be summaries only: names, counts,
    pseudonymous refs. Never candidate values or real IDs."""
    if trace_enabled():
        _emit({"type": "step", "stage": stage, "name": name, "details": details})


# --- Agent calls ---

def _record(agent: Agent, payload: str, reply: Any, ok: bool, start: float, seconds: float,
            log_dir: Path | None) -> None:
    global _calls
    lang, shown = _pretty(payload)
    with _lock:
        _calls += 1
        n = _calls
    text = json.dumps(reply, indent=2, ensure_ascii=False, default=str) if ok else str(reply)
    md = (
        f"\n---\n\n## {n}. {agent.name}\n\n"
        f"{datetime.fromtimestamp(start):%H:%M:%S} · {seconds:.1f}s · model: {agent.model or 'SDK default'} · "
        f"{'ok' if ok else 'FAILED'}\n\n"
        f"### Instructions\n\n```text\n{agent.instructions}\n```\n\n"
        f"### Input\n\n```{lang}\n{shown}\n```\n\n"
        f"### {'Output' if ok else 'Error'}\n\n```{'json' if ok else 'text'}\n{text}\n```\n"
    )
    event = {"type": "agent", "seq": n, "agent": agent.name, "model": agent.model or "SDK default",
             "start": start, "seconds": round(seconds, 3), "ok": ok, "instructions": agent.instructions,
             "input": _as_data(payload), ("output" if ok else "error"): reply}
    _emit(event, md, log_dir)


async def run_with_retries(agent: Agent, payload: str, output_type: type[T], tag: str, attempts: int = 2) -> T:
    """`run_agent`, retried on failure, with the usual log lines under `[tag]`.

    Raises:
        RuntimeError: If every attempt fails.
    """
    logger.info("[%s] Calling %s (model: %s)", tag, agent.name, agent.model or "SDK default")
    logger.debug("[%s] Payload:\n%s", tag, payload)
    last_error = None
    for attempt in range(1, attempts + 1):
        start = time.perf_counter()
        try:
            output = await run_agent(agent, payload, output_type)
            logger.info("[%s] Done in %.1fs (attempt %d)", tag, time.perf_counter() - start, attempt)
            logger.debug("[%s] Output:\n%s", tag, output.model_dump_json(indent=2))
            return output
        except Exception as e:  # retry on API/parse failures
            last_error = e
            logger.warning("[%s] Attempt %d/%d failed after %.1fs: %s",
                           tag, attempt, attempts, time.perf_counter() - start, e)
    raise RuntimeError(f"{agent.name} failed after {attempts} attempts: {last_error}")


async def run_agent(agent: Agent, payload: str, output_type: type[T], log_dir: Path | None = None) -> T:
    """Run `agent` on `payload` once and return its structured output.

    Callers keep their own retries; each attempt is one entry in the trace.

    Raises:
        Whatever the SDK raises (API errors, timeouts), or TypeError if the reply isn't an
        `output_type`.
    """
    wall, start = time.time(), time.perf_counter()
    try:
        result = await Runner.run(agent, payload)
        output = result.final_output_as(output_type, raise_if_incorrect_type=True)
    except Exception as e:
        if trace_enabled():
            _record(agent, payload, f"{type(e).__name__}: {e}", False, wall, time.perf_counter() - start, log_dir)
        raise
    if trace_enabled():
        reply = output.model_dump(mode="json") if isinstance(output, BaseModel) else str(output)
        _record(agent, payload, reply, True, wall, time.perf_counter() - start, log_dir)
    return output


# --- Reading traces back (for the /runs page) ---

def _events(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue   # a line cut off by a crash; skip it
    return events


def summarize_trace(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The operations in a trace, oldest first, with timing, status and agent-call counts.
    Events outside any operation are grouped as one "(no operation)" entry."""
    ops: dict[str | None, dict[str, Any]] = {}
    for e in events:
        op = ops.setdefault(e.get("op"), {"id": e.get("op"), "name": "(no operation)", "detail": "",
                                         "started": e.get("time"), "seconds": None, "ok": None,
                                         "agent_calls": 0, "failed_calls": 0, "steps": 0})
        if e["type"] == "operation_start":
            op.update(name=e.get("name", ""), detail=e.get("detail", ""), started=e.get("time"))
        elif e["type"] == "operation_end":
            op.update(seconds=e.get("seconds"), ok=e.get("ok"), error=e.get("error"))
        elif e["type"] == "agent":
            op["agent_calls"] += 1
            op["failed_calls"] += not e.get("ok", True)
        elif e["type"] == "step":
            op["steps"] += 1
    return list(ops.values())


def list_traces(log_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every .jsonl trace in `log_dir`, newest first, with its operations."""
    log_dir = log_dir or LOG_DIR
    if not log_dir.exists():
        return []
    traces = []
    for path in sorted(log_dir.glob("trace-*.jsonl"), reverse=True):
        if TRACE_NAME.match(path.name):
            traces.append({"name": path.name, "size": path.stat().st_size,
                           "operations": summarize_trace(_events(path))})
    return traces


def read_trace(name: str, log_dir: Path | None = None) -> list[dict[str, Any]]:
    """All events of one trace file.

    Raises:
        FileNotFoundError: For a name that isn't a trace file in `log_dir`.
    """
    path = (log_dir or LOG_DIR) / Path(name).name
    if not TRACE_NAME.match(path.name) or not path.exists():
        raise FileNotFoundError(f"No trace named {name!r}.")
    return _events(path)
