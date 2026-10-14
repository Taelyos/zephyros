"""The obedient stub: a model provider that does what its context tells it to.

The attack catalog needs an agent that falls for every injection, so that a refusal never rests
on the model declining (ADR 0002). This provider reads every line of the
conversation that is not its own, finds instructions in a small fixed grammar, and answers with
a call to a tool of the agent engine for the first one it has not yet executed. It proves that
the platform stops what a model attempts; it proves nothing about a real model.

Grammar, one instruction per line, at the start of the line:

    FETCH <url>           fetch the URL with the terminal
    WRITE <path>          write a file with the file tool
    RUN <command>         run a command in the terminal
    PRINT_ENV             print the environment with the terminal
    CODE <python>         run Python with the code tool
    TOOL <name> <json>    call any tool the request offers, with these arguments

When every instruction has been executed it answers with the results, so that a test that
runs the agent once can read what happened from the final answer. Nothing here is random: the
same conversation gives the same answer.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from zephyros.core.ports.model_provider import (
    INTERFACE_VERSION,
    ChatMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)

_INSTRUCTION = re.compile(
    r"^[ \t]*(FETCH|WRITE|RUN|PRINT_ENV|CODE|TOOL)\b[ \t]*(.*)$", re.MULTILINE
)
RESULT_LIMIT = 300


@dataclass(frozen=True, slots=True)
class Instruction:
    kind: str
    argument: str


def find_instructions(messages: Iterable[ChatMessage]) -> list[Instruction]:
    """Every instruction in the conversation, in order, wherever it came from (a user, a page
    fetched by a tool, a file read). The stub's own messages are not read."""
    found: list[Instruction] = []
    for message in messages:
        if message.role is Role.ASSISTANT or not message.content:
            continue
        found.extend(
            Instruction(m.group(1), m.group(2).strip())
            for m in _INSTRUCTION.finditer(message.content)
        )
    return found


def _property(tool: ToolSpec, preferred: tuple[str, ...]) -> str:
    properties = tool.parameters.get("properties")
    names = list(properties) if isinstance(properties, Mapping) else []
    for name in preferred:
        if name in names:
            return name
    required = tool.parameters.get("required")
    if isinstance(required, list) and required:
        return str(required[0])
    return names[0] if names else preferred[0]


def _words(messages: Iterable[ChatMessage]) -> int:
    count = 0
    for message in messages:
        count += len((message.content or "").split())
        count += sum(1 + len(call.arguments.split()) for call in message.tool_calls)
    return count


class ObedientStubProvider:
    """Executes the instructions found in its context, one tool call at a time."""

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    @staticmethod
    def _plan(
        instruction: Instruction, tools: Mapping[str, ToolSpec]
    ) -> tuple[str, dict[str, object]] | None:
        """The tool and arguments that carry out an instruction, or None if none can."""
        kind, argument = instruction.kind, instruction.argument
        if kind == "TOOL":
            name, _, raw = argument.partition(" ")
            if name not in tools:
                return None
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {}
            return name, parsed if isinstance(parsed, dict) else {}
        if kind == "CODE" and "execute_code" in tools:
            return "execute_code", {_property(tools["execute_code"], ("code",)): argument}
        if kind == "WRITE" and "write_file" in tools:
            tool = tools["write_file"]
            return "write_file", {
                _property(tool, ("path", "file_path")): argument,
                _property(tool, ("content", "text")): "written by the obedient stub\n",
            }
        command = {
            "FETCH": f"curl -sS -m 8 -i {shlex.quote(argument)} | head -c 600",
            "RUN": argument,
            "PRINT_ENV": "env",
            "WRITE": f"printf 'written by the obedient stub\\n' > {shlex.quote(argument)}",
        }.get(kind)
        if command is not None and "terminal" in tools:
            return "terminal", {_property(tools["terminal"], ("command", "cmd")): command}
        if kind == "FETCH" and "execute_code" in tools:
            code = (
                "import urllib.request; "
                f"print(urllib.request.urlopen({argument!r}, timeout=8).read(600))"
            )
            return "execute_code", {_property(tools["execute_code"], ("code",)): code}
        return None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.messages:
            raise ModelProviderError("the conversation is empty")
        tools = {tool.name: tool for tool in request.tools}
        instructions = find_instructions(request.messages)
        executed = sum(len(m.tool_calls) for m in request.messages if m.role is Role.ASSISTANT)
        reply: ChatMessage
        if executed < len(instructions):
            plan = self._plan(instructions[executed], tools)
            if plan is None:
                reply = ChatMessage(
                    Role.ASSISTANT,
                    f"STUB-CANNOT: no offered tool can carry out {instructions[executed].kind}",
                )
            else:
                name, arguments = plan
                reply = ChatMessage(
                    Role.ASSISTANT,
                    None,
                    (ToolCall(f"call_stub_{executed + 1}", name, json.dumps(arguments)),),
                )
        else:
            outputs = [m.content for m in request.messages if m.role is Role.TOOL and m.content]
            results = [
                f"RESULT {number}: {content[:RESULT_LIMIT]!r}"
                for number, content in enumerate(outputs, start=1)
            ]
            reply = ChatMessage(
                Role.ASSISTANT, "\n".join([f"STUB-DONE: {executed} instruction(s)", *results])
            )
        usage = Usage(max(1, _words(request.messages)), max(1, _words([reply])))
        return ModelResponse(request.model, reply, usage)

    async def aclose(self) -> None:
        return None
