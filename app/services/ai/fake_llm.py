# ruff: noqa: E501, D401
"""Scripted Fake LLM (Epic 53, AD-57, FR-272, NFR-54): implements the Anthropic client surface the gateway uses
(``messages.create`` / ``messages.stream``) from a script of turns. Tests, CI, demos and keyless non-production
environments use it; it is never selected in production (settings refuse ``AI_PROVIDER=fake`` there).

Script: an ordered list of ``FakeTurn`` (or a mapping ``prompt_id -> turns``). Each model call consumes the next turn of
its prompt; an exhausted script answers ``emit_result {"outcome": "done", "summary": "script exhausted"}``.

Turn kinds: ``tool_calls`` (``calls=((tool key or wire name, input), ...)``), ``text`` (end_turn), ``emit_result``
(``result``), ``await_event`` (``wait``), ``refusal`` (``refusal_category``), ``truncated`` (max_tokens), ``error``
(``timeout`` | ``provider_error`` raised) and ``pause`` (pause_turn). ``expect(messages) -> bool`` asserts on the
incoming messages (False -> ``AssertionError``, surfaced by the gateway as ``provider_error`` and recorded in
``FakeLLM.failures``).

Keyless mode (``AI_PROVIDER=fake`` or no API key outside production): ``<ai_fake_script_dir>/<prompt_id>.yaml`` else
``default.yaml``; the turn index is the number of assistant messages already in the history (stateless across workers).
"""

import contextlib
import contextvars
import dataclasses
import itertools
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import yaml

from app.core.settings import BACKEND_DIR, get_settings

TurnKind = Literal[
    "tool_calls", "text", "emit_result", "await_event", "refusal", "truncated", "error", "pause"
]
ClientFactory = Callable[[str], Any]
FAKE_MODEL = "fake"
EXHAUSTED_RESULT = {"outcome": "done", "summary": "script exhausted"}

# Set by ``ai.loop`` around each call so the fake can pick the script of the calling prompt.
CURRENT_PROMPT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "talent_os_fake_prompt", default=None
)


@dataclasses.dataclass(frozen=True)
class FakeTurn:
    kind: TurnKind
    calls: tuple[tuple[str, dict[str, Any]], ...] = ()
    text: str = ""
    result: dict[str, Any] | None = None
    wait: dict[str, Any] | None = None
    refusal_category: str | None = None
    error: Literal["timeout", "provider_error"] | None = None
    expect: Callable[[list[dict[str, Any]]], bool] | None = None
    usage: tuple[int, int] = (100, 50)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FakeTurn":
        calls = tuple((str(name), dict(args or {})) for name, args in (raw.get("calls") or ()))
        usage = raw.get("usage") or (100, 50)
        return cls(
            kind=raw["kind"],
            calls=calls,
            text=str(raw.get("text") or ""),
            result=raw.get("result"),
            wait=raw.get("wait"),
            refusal_category=raw.get("refusal_category"),
            error=raw.get("error"),
            usage=(int(usage[0]), int(usage[1])),
        )


def _wire(name: str) -> str:
    return name.replace(".", "__") if "." in name else name


def _usage(turn: FakeTurn) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=turn.usage[0],
        output_tokens=turn.usage[1],
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


class _Counter:
    def __init__(self) -> None:
        self._n = itertools.count(1)

    def next_id(self) -> str:
        return f"toolu_fake_{next(self._n)}"


def build_message(turn: FakeTurn, ids: _Counter) -> SimpleNamespace:
    """The SDK-shaped ``Message`` for one turn (raises for ``error`` turns)."""
    if turn.kind == "error":
        if turn.error == "timeout":
            raise TimeoutError("fake timeout")
        raise RuntimeError("fake provider error")
    content: list[SimpleNamespace] = []
    stop_reason = "end_turn"
    stop_details = None
    if turn.text:
        content.append(SimpleNamespace(type="text", text=turn.text))
    if turn.kind == "tool_calls":
        for name, args in turn.calls:
            content.append(
                SimpleNamespace(type="tool_use", id=ids.next_id(), name=_wire(name), input=dict(args))
            )
        stop_reason = "tool_use"
    elif turn.kind == "emit_result":
        content.append(
            SimpleNamespace(
                type="tool_use",
                id=ids.next_id(),
                name="emit_result",
                input=dict(turn.result or EXHAUSTED_RESULT),
            )
        )
        stop_reason = "tool_use"
    elif turn.kind == "await_event":
        content.append(
            SimpleNamespace(
                type="tool_use", id=ids.next_id(), name="await_event", input=dict(turn.wait or {})
            )
        )
        stop_reason = "tool_use"
    elif turn.kind == "refusal":
        stop_reason = "refusal"
        stop_details = SimpleNamespace(
            type="refusal", category=turn.refusal_category or "other", explanation=None
        )
    elif turn.kind == "truncated":
        if not content:
            content.append(SimpleNamespace(type="text", text="(truncated)"))
        stop_reason = "max_tokens"
    elif turn.kind == "pause":
        if not content:
            content.append(SimpleNamespace(type="text", text="(paused)"))
        stop_reason = "pause_turn"
    elif turn.kind == "text" and not content:
        content.append(SimpleNamespace(type="text", text="done"))
    return SimpleNamespace(
        id=f"msg_fake_{ids.next_id()}",
        type="message",
        role="assistant",
        model=FAKE_MODEL,
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=_usage(turn),
    )


class _Stream:
    def __init__(self, message: SimpleNamespace) -> None:
        self._message = message

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        return self._words()

    async def _words(self) -> AsyncIterator[str]:
        for block in self._message.content:
            if getattr(block, "type", None) == "text":
                words = block.text.split(" ")
                for i, word in enumerate(words):
                    yield word if i == len(words) - 1 else f"{word} "

    async def get_final_message(self) -> SimpleNamespace:
        return self._message


class FakeLLM:
    def __init__(self, script: Sequence[FakeTurn] | Mapping[str, Sequence[FakeTurn]]) -> None:
        if isinstance(script, Mapping):
            self._by_prompt: dict[str | None, list[FakeTurn]] = {k: list(v) for k, v in script.items()}
        else:
            self._by_prompt = {None: list(script)}
        self._pos: dict[str | None, int] = {}
        self.calls: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self._ids = _Counter()

    def _next(self) -> FakeTurn:
        prompt = CURRENT_PROMPT.get()
        key = prompt if prompt in self._by_prompt else None
        turns = self._by_prompt.get(key, [])
        pos = self._pos.get(key, 0)
        self._pos[key] = pos + 1
        if pos < len(turns):
            return turns[pos]
        return FakeTurn(kind="emit_result", result=dict(EXHAUSTED_RESULT))

    def _respond(self, kwargs: dict[str, Any]) -> SimpleNamespace:
        self.calls.append(kwargs)
        turn = self._next()
        if turn.expect is not None and not turn.expect(list(kwargs.get("messages") or [])):
            self.failures.append(f"expect failed at call {len(self.calls)}")
            msg = f"FakeLLM expectation failed at call {len(self.calls)}"
            raise AssertionError(msg)
        return build_message(turn, self._ids)

    def factory(self) -> ClientFactory:
        fake = self

        class _Messages:
            async def create(self, **kwargs: Any) -> Any:
                return fake._respond(kwargs)

            def stream(self, **kwargs: Any) -> _Stream:
                return _Stream(fake._respond(kwargs))

        def make(api_key: str) -> Any:  # noqa: ARG001 - the fake needs no key
            return SimpleNamespace(messages=_Messages())

        return make

    @classmethod
    def from_yaml(cls, path: Path) -> "FakeLLM":
        return cls(load_script(path))


def load_script(path: Path) -> list[FakeTurn] | dict[str, list[FakeTurn]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if isinstance(data, Mapping) and "turns" in data:
        data = data["turns"]
    if isinstance(data, Mapping):
        return {str(k): [FakeTurn.from_mapping(t) for t in v] for k, v in data.items()}
    return [FakeTurn.from_mapping(t) for t in data]


# ---------------------------------------------------------------- keyless mode (stateless, YAML per prompt)
def _script_dir() -> Path:
    raw = Path(get_settings().ai_fake_script_dir)
    return raw if raw.is_absolute() else BACKEND_DIR / raw


def keyless_factory() -> ClientFactory:
    """Stateless client: turn index = number of assistant messages already in the history."""
    ids = _Counter()

    def turns_for(prompt: str | None) -> list[FakeTurn]:
        base = _script_dir()
        for candidate in ([base / f"{prompt}.yaml"] if prompt else []) + [base / "default.yaml"]:
            if candidate.exists():
                script = load_script(candidate)
                if isinstance(script, dict):
                    return script.get(prompt or "", []) or next(iter(script.values()), [])
                return script
        return []

    def respond(kwargs: dict[str, Any]) -> SimpleNamespace:
        prompt = CURRENT_PROMPT.get()
        turns = turns_for(prompt)
        index = sum(1 for m in kwargs.get("messages") or [] if m.get("role") == "assistant")
        turn = (
            turns[index]
            if index < len(turns)
            else FakeTurn(kind="emit_result", result=dict(EXHAUSTED_RESULT))
        )
        return build_message(turn, ids)

    class _Messages:
        async def create(self, **kwargs: Any) -> Any:
            return respond(kwargs)

        def stream(self, **kwargs: Any) -> _Stream:
            return _Stream(respond(kwargs))

    def make(api_key: str) -> Any:  # noqa: ARG001
        return SimpleNamespace(messages=_Messages())

    return make


@contextlib.contextmanager
def fake_llm(script: Sequence[FakeTurn] | Mapping[str, Sequence[FakeTurn]]) -> Iterator[FakeLLM]:
    """Install a ``FakeLLM`` as the gateway client for the block (restores the previous factory)."""
    from app.services.ai import gateway

    previous = gateway._client_factory
    fake = FakeLLM(script)
    gateway.set_client_factory(fake.factory())
    try:
        yield fake
    finally:
        gateway.set_client_factory(previous)


def install_fake(script: Sequence[FakeTurn] | Mapping[str, Sequence[FakeTurn]]) -> FakeLLM:
    """Install a ``FakeLLM`` until ``ai.set_client_factory(None)`` (tests' fixtures restore it)."""
    from app.services.ai import gateway

    fake = FakeLLM(script)
    gateway.set_client_factory(fake.factory())
    return fake


__all__ = [
    "CURRENT_PROMPT",
    "FAKE_MODEL",
    "FakeLLM",
    "FakeTurn",
    "build_message",
    "fake_llm",
    "install_fake",
    "keyless_factory",
    "load_script",
]
