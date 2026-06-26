"""AgentBase: the shared LLM-agent framework (architecture.md §4).

Each SENTINEL agent is a model context with its own system prompt, tool
allowlist, and (optionally) a structured-output schema. ``AgentBase`` provides
the three pieces every agent needs:

  * system-prompt loading (from a bundled prompt file or an inline string),
  * MCP tool binding per agent (a per-agent allowlist of :class:`Tool` objects,
    offered to the model as function-calls and dispatched on request), and
  * structured-output parsing into the project's Pydantic schemas (which, for
    ``Proposal``/``Veto``/``RiskProfile``, is also where Golden Rule #1's
    ``trace_ids`` requirement is enforced — an unsourced claim fails to parse).

The LLM is reached through the :class:`ChatClient` protocol, so agents are
fully testable with a scripted fake — no network in unit tests.
"""

from __future__ import annotations

import inspect
import json
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ValidationError

from sentinel.observability.trace_logger import get_logger
from sentinel.orchestrator.schema import AgentRole

_DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PY_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


class _Unset:
    """Sentinel distinguishing 'argument omitted' from an explicit ``None``."""


_UNSET = _Unset()


class AgentError(RuntimeError):
    """Base error for agent execution failures."""


class AgentOutputError(AgentError):
    """Raised when the model output cannot be parsed into the expected schema."""


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    """A single model turn: free-text content and/or tool calls."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class ChatClient(Protocol):
    """The minimal LLM interface AgentBase depends on (see ``QwenClient``)."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Run one chat completion and return the model's turn."""
        ...


@dataclass
class Tool:
    """A model-callable tool: an OpenAI function schema plus its executor."""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]

    def to_openai(self) -> dict[str, Any]:
        """Render this tool as an OpenAI-compatible ``tools`` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def call(self, arguments: dict[str, Any]) -> Any:
        """Execute the tool with keyword ``arguments``."""
        return self.func(**arguments)


def _json_type(annotation: Any) -> dict[str, Any]:
    """Map a Python type annotation to a JSON-schema fragment."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        return _json_type(non_none[0]) if non_none else {"type": "string"}
    if origin in (list, tuple):
        args = get_args(annotation)
        item = _json_type(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item}
    if annotation in _PY_TO_JSON:
        return {"type": _PY_TO_JSON[annotation]}
    return {"type": "string"}


def tool_from_callable(
    func: Callable[..., Any], name: str | None = None, description: str | None = None
) -> Tool:
    """Build a :class:`Tool` from a typed Python callable.

    The JSON-schema parameters are derived from the callable's signature and
    type hints; parameters without a default are marked required. This is how an
    MCP tool function (e.g. a SimulationMCP/CodebaseMCP/MemoryMCP tool) is bound
    onto an agent.

    Args:
        func: The callable to expose.
        name: Tool name (defaults to ``func.__name__``).
        description: Tool description (defaults to the first docstring line).

    Returns:
        A :class:`Tool` ready to bind onto an agent.
    """
    hints = get_type_hints(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in inspect.signature(func).parameters.items():
        if param_name == "self":
            continue
        properties[param_name] = _json_type(hints.get(param_name, str))
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
    doc = (func.__doc__ or "").strip().split("\n", 1)[0]
    return Tool(
        name=name or func.__name__,
        description=description or doc,
        parameters={"type": "object", "properties": properties, "required": required},
        func=func,
    )


class AgentBase:
    """Base class for SENTINEL LLM agents (architecture.md §4)."""

    def __init__(
        self,
        *,
        role: AgentRole,
        client: ChatClient,
        model: str,
        system_prompt: str,
        tools: list[Tool] | None = None,
        output_schema: type[BaseModel] | None = None,
        max_tool_iterations: int = 6,
        max_output_repairs: int = 1,
    ) -> None:
        """Configure an agent.

        Args:
            role: Which SENTINEL agent this is.
            client: The LLM client (real ``QwenClient`` or a test fake).
            model: The model id for this agent (from config/.env).
            system_prompt: The agent's system prompt.
            tools: The agent's bound tool allowlist (MCP tools), if any.
            output_schema: If set, ``run`` returns a parsed instance of this
                Pydantic model rather than raw text.
            max_tool_iterations: Cap on tool-call rounds (prevents loops).
            max_output_repairs: How many times to re-ask on an unparseable reply.
        """
        self.role = role
        self._client = client
        self._model = model
        self._tools = tools or []
        self._tools_by_name = {t.name: t for t in self._tools}
        self._output_schema = output_schema
        self._max_tool_iterations = max_tool_iterations
        self._max_output_repairs = max_output_repairs
        self._log = get_logger(f"agent.{role.value}")
        self._base_system_prompt = system_prompt

    @property
    def model(self) -> str:
        """The model id this agent calls (for observability / token tiering)."""
        return self._model

    @staticmethod
    def load_prompt(name: str, prompts_dir: Path | None = None) -> str:
        """Load a system prompt by name from the prompts directory.

        Args:
            name: Prompt file stem (``<name>.md``).
            prompts_dir: Directory to read from (defaults to the bundled one).

        Returns:
            The prompt text.

        Raises:
            FileNotFoundError: If the prompt file does not exist.
        """
        path = (prompts_dir or _DEFAULT_PROMPTS_DIR) / f"{name}.md"
        return path.read_text()

    def run(
        self,
        user_message: str,
        *,
        output_schema: type[BaseModel] | None | _Unset = _UNSET,
    ) -> BaseModel | str:
        """Run the agent on one user message, returning text or a parsed model.

        Drives the tool-call loop: while the model requests tools, dispatch them
        and feed results back; once it answers, return the text or parse it into
        the effective output schema. An unparseable structured reply is re-asked
        up to ``max_output_repairs`` times before failing.

        Args:
            user_message: The task/context for this turn.
            output_schema: Per-call override of the agent's default output
                schema — lets a multi-phase agent (e.g. the Arbitrator, which
                emits a ``Proposal`` mid-loop and a ``RiskProfile`` at the end)
                reuse one configured agent. Omit to use the construction-time
                schema; pass ``None`` to force a plain-text turn.

        Returns:
            The parsed Pydantic model (if a schema is in effect) else text.

        Raises:
            AgentError: If the tool loop exceeds its cap.
            AgentOutputError: If a structured reply cannot be parsed.
        """
        schema = (
            self._output_schema if isinstance(output_schema, _Unset) else output_schema
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._compose_system_prompt(
                    self._base_system_prompt, schema
                ),
            },
            {"role": "user", "content": user_message},
        ]
        tool_trace_ids: list[str] = []
        repairs_left = self._max_output_repairs
        for _ in range(self._max_tool_iterations):
            tool_schemas = [t.to_openai() for t in self._tools] or None
            response = self._client.chat(messages, self._model, tools=tool_schemas)

            if response.tool_calls:
                messages.append(_assistant_message(response))
                for call in response.tool_calls:
                    result = self._dispatch(call)
                    tool_trace_ids += _collect_trace_ids(result)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": _json_dumps(result),
                        }
                    )
                continue

            if schema is None:
                return response.content or ""
            try:
                parsed = self._parse_output(response.content, schema)
                return self._source_model_trace_ids(parsed, tool_trace_ids)
            except AgentOutputError as exc:
                if repairs_left <= 0:
                    raise
                repairs_left -= 1
                messages.append(
                    {"role": "assistant", "content": response.content or ""}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That was not valid JSON for the required schema. "
                            f"Validation error: {exc}. Reply with ONLY the JSON "
                            "object, no prose or fences."
                        ),
                    }
                )

        raise AgentError(
            f"{self.role.value}: exceeded {self._max_tool_iterations} tool rounds"
        )

    def _dispatch(self, call: ToolCall) -> Any:
        """Execute a requested tool, degrading gracefully on failure (Rule 4)."""
        tool = self._tools_by_name.get(call.name)
        if tool is None:
            self._log.warning("unknown tool requested", tool=call.name)
            return {"error": f"unknown tool {call.name!r}"}
        try:
            self._log.info("tool_call", tool=call.name, arguments=call.arguments)
            return tool.call(call.arguments)
        except Exception as exc:  # noqa: BLE001 — tool failure degrades, run continues
            self._log.error(
                "[DEGRADED] tool call failed", tool=call.name, error=str(exc)
            )
            result = {"error": str(exc)}
            trace_id = getattr(exc, "trace_id", None)
            if trace_id:
                result["trace_id"] = trace_id
            return result

    def _parse_output(self, content: str | None, schema: type[BaseModel]) -> BaseModel:
        """Parse model text into ``schema`` (enforces e.g. trace_ids)."""
        if content is None:
            raise AgentOutputError(f"{self.role.value}: model returned no content")
        text = _extract_json(content)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentOutputError(
                f"{self.role.value}: reply was not valid JSON: {exc}"
            ) from exc
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            raise AgentOutputError(
                f"{self.role.value}: reply did not match {schema.__name__}: {exc}"
            ) from exc

    def _source_model_trace_ids(
        self, parsed: BaseModel, tool_trace_ids: list[str]
    ) -> BaseModel:
        """Clamp model-supplied trace_ids to real traces from this tool turn."""
        if not tool_trace_ids or not hasattr(parsed, "trace_ids"):
            return parsed
        cited = list(parsed.trace_ids)
        known = set(tool_trace_ids)
        valid = [trace_id for trace_id in cited if trace_id in known]
        if valid:
            if valid == cited:
                return parsed
            self._log.debug(
                "model cited unknown trace_ids",
                cited=cited,
                valid=valid,
            )
            return parsed.model_copy(update={"trace_ids": valid})
        fallback = tool_trace_ids[-1:]
        self._log.debug(
            "model cited unknown trace_ids",
            cited=cited,
            fallback=fallback,
        )
        return parsed.model_copy(update={"trace_ids": fallback})

    @staticmethod
    def _compose_system_prompt(
        system_prompt: str, schema: type[BaseModel] | None
    ) -> str:
        """Append a JSON-schema directive when a structured output is required."""
        if schema is None:
            return system_prompt
        json_schema = json.dumps(schema.model_json_schema())
        return (
            f"{system_prompt}\n\nYou MUST respond with ONLY a single JSON object "
            f"that validates against this JSON Schema, with no prose or code "
            f"fences:\n{json_schema}"
        )


def _assistant_message(response: ChatResponse) -> dict[str, Any]:
    """Reconstruct the OpenAI assistant message that carried the tool calls."""
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in response.tool_calls
        ],
    }


def _json_dumps(obj: Any) -> str:
    """JSON-encode a tool result, tolerating Pydantic models and odd types."""

    def _default(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        return str(value)

    return json.dumps(obj, default=_default)


def _collect_trace_ids(value: Any) -> list[str]:
    """Extract trace_id fields from tool results, including nested models."""
    if isinstance(value, BaseModel):
        return _collect_trace_ids(value.model_dump(mode="json"))
    if isinstance(value, dict):
        found: list[str] = []
        trace_id = value.get("trace_id")
        if isinstance(trace_id, str) and trace_id.strip():
            found.append(trace_id)
        for child in value.values():
            found += _collect_trace_ids(child)
        return _dedup(found)
    if isinstance(value, (list, tuple)):
        list_found: list[str] = []
        for child in value:
            list_found += _collect_trace_ids(child)
        return _dedup(list_found)
    return []


def _dedup(items: list[str]) -> list[str]:
    """Order-preserving de-duplication."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _extract_json(text: str) -> str:
    """Pull a JSON object out of model text (handles code fences and prose)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped[3:]
        if body[:4].lower() == "json":
            body = body[4:]
        return body.split("```", 1)[0].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped
