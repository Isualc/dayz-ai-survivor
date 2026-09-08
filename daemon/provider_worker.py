"""Persistent stream-json bridge for CLI and direct API DayZ brains.

Only this process executes tools, from one explicitly configured DayZ MCP
server plus confined personal-memory files. All LLM output is untrusted JSON.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid

from llm_backends import (API_DEFAULTS, ApiPlanner, BackendError, CliPlanner,
                          DECISION_RULES, Completion, parse_decision, validate_backend)


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def clipped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[truncated: request a narrower observation]"


def bounded_int(env: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(env.get(key, default))))
    except (ValueError, TypeError):
        raise BackendError(f"{key} must be an integer.") from None


MEMORY_TOOLS = [
    {"name": "Read", "description": "Read personal memory only: CLAUDE.md or memory/<name>.md. No other host files.",
     "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string"}},
                     "required": ["file_path"], "additionalProperties": False}},
    {"name": "Write", "description": "Save evidence-based personal notes to CLAUDE.md or memory/<name>.md. This replaces that note.",
     "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
                     "required": ["file_path", "content"], "additionalProperties": False}},
]


class MemoryFiles:
    def __init__(self, home: str | Path):
        self.home = Path(home).resolve()

    def path(self, value: str) -> Path:
        if not isinstance(value, str) or not value or ":" in value.replace(str(self.home), "", 1):
            raise BackendError("Memory path must be CLAUDE.md or memory/<name>.md.")
        candidate = Path(value)
        path = (candidate if candidate.is_absolute() else self.home / candidate).resolve()
        try:
            relative = path.relative_to(self.home)
        except ValueError:
            raise BackendError("Memory paths outside this survivor's home are forbidden.") from None
        if relative.as_posix() == "CLAUDE.md":
            return path
        if len(relative.parts) != 2 or relative.parts[0] != "memory" or path.suffix != ".md":
            raise BackendError("Memory path must be CLAUDE.md or memory/<name>.md.")
        return path

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        try:
            path = self.path(arguments.get("file_path", ""))
            if name == "Read":
                if set(arguments) != {"file_path"}:
                    raise BackendError("Read accepts only file_path.")
                return clipped(path.read_text(encoding="utf-8"), 6000), False
            if set(arguments) != {"file_path", "content"} or not isinstance(arguments["content"], str):
                raise BackendError("Write requires file_path and text content.")
            if len(arguments["content"]) > 12000:
                raise BackendError("Memory note exceeds 12000 characters; keep durable lessons concise.")
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".provider-tmp")
            # Resolve the temporary path too, so an existing symlink cannot
            # redirect a write outside the allowed memory directory.
            if temp.is_symlink() or temp.resolve().parent != path.parent:
                raise BackendError("Unsafe temporary memory path.")
            temp.write_text(arguments["content"], encoding="utf-8")
            os.replace(temp, path)
            return "Personal memory saved.", False
        except (BackendError, OSError, UnicodeError) as exc:
            return str(exc) if isinstance(exc, BackendError) else "Memory file unavailable.", True

    def briefing(self) -> str:
        chunks = []
        paths = [self.home / "CLAUDE.md"]
        memory = self.home / "memory"
        if memory.is_dir():
            paths += sorted(memory.glob("*.md"))[:12]
        remaining = 9000
        for candidate in paths:
            try:
                path = self.path(str(candidate))
                text = clipped(path.read_text(encoding="utf-8"), min(3000, remaining))
            except (BackendError, OSError, UnicodeError):
                continue
            chunks.append(str(path.relative_to(self.home)) + ":\n" + text)
            remaining -= len(text)
            if remaining <= 0:
                break
        return "\n\n".join(chunks)


class DayzTools:
    def __init__(self, config_file: str, home: str, timeout: int = 260):
        self.config_file = config_file
        self.home = home
        self.timeout = timeout
        self.memory = MemoryFiles(home)
        self.stack = AsyncExitStack()
        self.session = None
        self.catalog = []

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        try:
            config = json.loads(Path(self.config_file).read_text(encoding="utf-8"))
            dayz = config["mcpServers"]["dayz"]
            # Only this named server is read. Global CLI servers are irrelevant.
            server = StdioServerParameters(command=dayz["command"], args=dayz.get("args", []),
                                           env={**os.environ, **dayz.get("env", {})}, cwd=self.home)
            streams = await self.stack.enter_async_context(stdio_client(server, errlog=sys.stderr))
            self.session = await self.stack.enter_async_context(ClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=self.timeout)))
            await self.session.initialize()
            cursor = None
            while True:
                page = await self.session.list_tools(cursor=cursor)
                self.catalog += [{"name": t.name, "description": (t.description or "")[:260],
                                  "inputSchema": t.inputSchema} for t in page.tools]
                cursor = page.nextCursor
                if not cursor:
                    break
            self.catalog += MEMORY_TOOLS
            selected = os.environ.get("ISU_LLM_ALLOWED_TOOLS", "").strip()
            if selected:
                allowed = {name.strip() for name in selected.split(",") if name.strip()}
                unknown = allowed - {t["name"] for t in self.catalog}
                if unknown:
                    raise BackendError("ISU_LLM_ALLOWED_TOOLS contains unknown tool names.")
                self.catalog = [t for t in self.catalog if t["name"] in allowed]
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *exc):
        await self.stack.aclose()

    async def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        if name not in {t["name"] for t in self.catalog}:
            raise BackendError("Tool outside the active allowlist.")
        if name in ("Read", "Write"):
            return self.memory.call(name, arguments)
        result = await asyncio.wait_for(self.session.call_tool(name, arguments), self.timeout)
        text = "\n".join(block.text for block in result.content if getattr(block, "type", "") == "text")
        return clipped(text or "Tool returned no text.", 6000), bool(result.isError)


# Tools, die die Welt nur LESEN: identisch wiederholen bringt nie Neues, darum
# bleibt hier die strenge Grenze (zwei Aufrufe pro Zug).
READ_ONLY_TOOLS = frozenset(("observe", "find_item", "Read", "recipes", "status"))
# Absolute Obergrenze identischer Aufrufe pro Zug, auch bei Fortschritt.
REPEAT_HARD_CAP = 5


def repeat_guard(name: str, history: list) -> str:
    """Stop-Grund für einen erneut identischen Tool-Aufruf, sonst "".

    history = [(failed, result_text), ...] der bisherigen Aufrufe mit exakt
    dieser Signatur (Tool + Argumente) im laufenden Zug.

    Früher zählte NUR die Signatur: der dritte explore_step {} wurde
    gestoppt, obwohl jeder Schritt einen anderen Ort erreicht und anderes
    Loot gebracht hatte (Igor/Konrad 08.09.) - und der Stopp erschien als
    Backend-Fehler. Jetzt zählt, ob die letzten beiden identischen Aufrufe
    etwas GEBRACHT haben: zwei Fehlschläge oder zweimal dasselbe Ergebnis =
    festgefahren; verschiedene Ergebnisse = Fortschritt (bis REPEAT_HARD_CAP).
    Reine Lese-Tools bleiben bei zwei Aufrufen.
    """
    n = len(history)
    if n >= REPEAT_HARD_CAP:
        return f"{name} was called {n} times this turn; end the turn and reassess with observe."
    if name in READ_ONLY_TOOLS and n >= 2:
        return f"{name} repeated {n} times without acting; act on what you see or end the turn."
    if n < 2:
        return ""
    (failed_a, result_a), (failed_b, result_b) = history[-2], history[-1]
    if failed_a and failed_b:
        return f"{name} failed twice with identical arguments; change the approach instead of retrying."
    if result_a == result_b:
        return f"{name} returned the same result twice; the action changes nothing - choose something else."
    return ""


class DecisionWorker:
    def __init__(self, model: str, planner, tools, persona: str, *, max_steps: int = 6,
                 context_chars: int = 30000, turn_seconds: int = 300,
                 emit_fn=emit, has_pending=lambda: False):
        self.model, self.planner, self.tools = model, planner, tools
        self.max_steps = max(1, min(32, max_steps))
        self.context_chars = max(6000, min(120000, context_chars))
        self.turn_seconds = max(30, min(900, turn_seconds))
        self.emit = emit_fn
        self.has_pending = has_pending
        self.turns: list[list[dict]] = []
        self.failure_count = 0
        self.retry_after = 0.0
        self.memory_briefing = ""
        self.allowed = {t["name"] for t in tools.catalog}
        self.system = (persona + "\n\n" + DECISION_RULES + "\nAVAILABLE TOOLS (JSON schemas):\n"
                       + json.dumps(tools.catalog, ensure_ascii=False, separators=(",", ":")))

    def messages(self, current: list[dict]) -> list[dict]:
        # Evict complete old turns, never cut a Kimi reasoning/tool exchange.
        while self.turns and len(json.dumps(self.turns + [current])) > self.context_chars:
            self.turns.pop(0)
        system = self.system
        if self.memory_briefing:
            system += "\n\nPersonal memory (past notes, re-check current world):\n" + self.memory_briefing
        return [{"role": "system", "content": system}] + [m for turn in self.turns for m in turn] + current

    async def turn(self, text: str) -> dict:
        started = time.monotonic()
        current = [{"role": "user", "content": clipped(text, 14000)}]
        steps, repeats = 0, {}
        subtype, error = "success", ""
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        try:
            if started < self.retry_after:
                raise BackendError(f"Provider cooling down after failure ({int(self.retry_after - started) + 1}s remaining); no request sent.")
            memory = getattr(self.tools, "memory", None)
            self.memory_briefing = memory.briefing() if memory else ""
            for _ in range(self.max_steps):
                if time.monotonic() - started >= self.turn_seconds:
                    subtype = "error_max_turns"
                    break
                if len(json.dumps(current)) > self.context_chars:
                    subtype = "error_max_turns"
                    break
                answer: Completion = await asyncio.to_thread(self.planner.complete, self.messages(current))
                steps += 1
                for key in totals:
                    totals[key] += int(answer.usage.get(key, 0))
                # Log usage even if the provider emits malformed decision JSON.
                usage_event = {"type": "assistant", "message": {
                    "id": uuid.uuid4().hex, "model": self.model,
                    "content": [], "usage": answer.usage}}
                self.emit(usage_event)
                summary, name, arguments = parse_decision(answer.text, self.allowed)
                current.append(answer.assistant or {"role": "assistant", "content": answer.text})
                if name and self.has_pending():
                    # A more recent world message is waiting. Acknowledge this
                    # turn separately; do not execute an already stale plan.
                    current.append({"role": "user", "content": "Planned tool was NOT executed: a newer world event interrupted this turn."})
                    break
                blocks = [{"type": "text", "text": summary}] if summary else []
                if not name:
                    if blocks:
                        self.emit({"type": "assistant", "message": {"content": blocks}})
                    break
                fingerprint = name + json.dumps(arguments, sort_keys=True)
                stop_note = repeat_guard(name, repeats.get(fingerprint, []))
                if stop_note:
                    # Festgefahrenes Modell ist KEIN Backend-Fehler: früher flog
                    # hier ein BackendError (Journal "FEHLER: error_backend" plus
                    # 15-300 s Provider-Pause), obwohl Provider und Werkzeuge in
                    # Ordnung waren. Jetzt endet der Zug regulär mit Hinweis.
                    self.emit({"type": "assistant", "message": {"content": [
                        {"type": "text", "text": "[HINWEIS] " + stop_note}]}})
                    current.append({"role": "user", "content": "Turn ended by the worker: " + stop_note})
                    break
                if time.monotonic() - started >= self.turn_seconds:
                    subtype = "error_max_turns"
                    break
                tool_id = uuid.uuid4().hex
                stream_name = name if name in ("Read", "Write") else "mcp__dayz__" + name
                blocks.append({"type": "tool_use", "id": tool_id, "name": stream_name, "input": arguments})
                self.emit({"type": "assistant", "message": {"content": blocks}})
                result, failed = await self.tools.call(name, arguments)
                result = clipped(result, 6000)
                repeats.setdefault(fingerprint, []).append((bool(failed), result))
                self.emit({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": result, "is_error": failed}]}})
                current.append({"role": "user", "content": json.dumps({"tool": name, "is_error": failed, "result": result}, ensure_ascii=False)})
                if self.has_pending():
                    break
            else:
                subtype = "error_max_turns"
        except BackendError as exc:
            subtype, error = "error_backend", str(exc)
        except Exception as exc:
            subtype, error = "error_backend", f"Game tool or provider unavailable ({type(exc).__name__}); reassess before retrying."
        if error:
            if started >= self.retry_after:
                self.failure_count += 1
                self.retry_after = time.monotonic() + min(300, 15 * 2 ** min(5, self.failure_count - 1))
            self.emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "[BACKEND] " + error}]}})
            current.append({"role": "user", "content": "Worker error: " + error})
        else:
            self.failure_count = 0
            self.retry_after = 0.0
        self.turns.append(current)
        while self.turns and len(json.dumps(self.turns)) > self.context_chars:
            self.turns.pop(0)
        result_event = {"type": "result", "subtype": subtype, "is_error": subtype != "success",
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "num_turns": steps, "usage": totals, "cost_known": False,
                        "billing": "provider-api" if self.model.split("/")[0] in API_DEFAULTS else "cli-account"}
        if error:
            result_event["errors"] = [error]
        self.emit(result_event)
        return result_event


def read_messages(stream, inbox: queue.Queue) -> None:
    try:
        for line in stream:
            try:
                value = json.loads(line)
                if value.get("type") != "user":
                    continue
                content = value.get("message", {}).get("content", [])
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
                else:
                    continue
                inbox.put(text)
            except (ValueError, TypeError, AttributeError):
                continue
    finally:
        inbox.put(None)


def has_queued_input(inbox: queue.Queue) -> bool:
    # EOF is not a new world event; a final piped user message still gets to act.
    with inbox.mutex:
        return any(item is not None for item in inbox.queue)


def install_child_job():
    """Windows: killing a worker also closes its MCP and CLI descendants."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                    ("active_processes", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        if handle:
            kernel.CloseHandle(handle)
        raise BackendError("Cannot create Windows process cleanup job.")
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        kernel.CloseHandle(handle)
        raise BackendError("Cannot attach worker to Windows process cleanup job.")
    # Keep this non-inheritable handle open for the worker lifetime. Windows
    # closes it even on terminate/kill, terminating all processes in the job.
    return handle


async def run(args) -> None:
    backend = validate_backend(args.model)
    timeout = bounded_int(os.environ, "ISU_LLM_REQUEST_TIMEOUT", 90, 10, 240)
    output_tokens = bounded_int(os.environ, "ISU_LLM_MAX_OUTPUT_TOKENS", 1200, 128, 16000)
    context_chars = bounded_int(os.environ, "ISU_LLM_CONTEXT_CHARS", 30000, 6000, 120000)
    # 300 statt 240: loot_area allein durfte 240 s laufen, damit kappte der
    # Zug nach einem einzigen Werkzeug (Igor 08.09.: 5 Schritte, 242 s,
    # error_max_turns). loot_area-Budget ist jetzt 180 s (tactics).
    turn_seconds = bounded_int(os.environ, "ISU_LLM_TURN_SECONDS", 300, 30, 900)
    # The runner's quiet watchdog is 300s; leave headroom even for regroup.
    tool_timeout = bounded_int(os.environ, "ISU_LLM_TOOL_TIMEOUT", 260, 10, 270)
    planner = (ApiPlanner(backend, timeout=timeout, max_tokens=output_tokens)
               if backend.provider in API_DEFAULTS else CliPlanner(backend, timeout=timeout))
    persona = Path(args.persona_file).read_text(encoding="utf-8")
    inbox: queue.Queue = queue.Queue(maxsize=128)
    threading.Thread(target=read_messages, args=(sys.stdin, inbox), daemon=True).start()
    async with DayzTools(args.mcp_config, args.agent_home, tool_timeout) as tools:
        worker = DecisionWorker(args.model, planner, tools, persona, max_steps=args.max_steps,
                                context_chars=context_chars, turn_seconds=turn_seconds,
                                has_pending=lambda: has_queued_input(inbox))
        emit({"type": "system", "subtype": "init", "model": args.model,
              "tools": [t["name"] for t in tools.catalog]})
        while True:
            message = await asyncio.to_thread(inbox.get)
            if message is None:
                break
            await worker.turn(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--mcp-config", required=True)
    parser.add_argument("--persona-file", required=True)
    parser.add_argument("--agent-home", required=True)
    parser.add_argument("--max-steps", type=int, default=6)
    args = parser.parse_args()
    try:
        cleanup_job = install_child_job()
        asyncio.run(run(args))
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, BackendError) else f"Provider startup failed ({type(exc).__name__})."
        print(message, file=sys.stderr, flush=True)
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "[BACKEND] " + message}]}})
        emit({"type": "result", "subtype": "error_backend", "is_error": True, "errors": [message],
              "num_turns": 0, "duration_ms": 0, "cost_known": False})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
