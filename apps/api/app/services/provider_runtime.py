from __future__ import annotations

from http.client import IncompleteRead
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.core.config import settings
from app.core.enums import ModelProviderType
from app.core.logging import log_event
from app.db.models.entities import ModelConfig

logger = logging.getLogger(__name__)


@dataclass
class ProviderResponse:
    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    raw_metadata: dict[str, Any]


@dataclass
class ToolCall:
    """A single tool invocation requested by the LLM."""

    tool_call_id: str
    tool_name: str
    tool_args: dict[str, Any]


@dataclass
class ProviderToolResponse:
    """Response from chat_with_tools. Enforces single-intent contract:
    - tool_calls non-empty  → intent is "execute these tools"
    - tool_calls empty AND content non-empty → intent is "final answer"
    - both empty → budget-exhausted / error
    """

    content: Optional[str]
    tool_calls: list[ToolCall]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    raw_metadata: dict[str, Any]
    finish_reason: str  # "stop", "tool_calls", "length"

    @property
    def is_tool_call(self) -> bool:
        return bool(self.tool_calls)

    @property
    def is_final_answer(self) -> bool:
        return not self.tool_calls and bool(self.content)


@dataclass
class ResolvedModelConfig:
    provider: str
    model_name: str
    endpoint: Optional[str]
    api_key: Optional[str]
    temperature: Optional[float]
    max_tokens: Optional[int]
    source: str
    llm_mode: str


class ProviderCallError(Exception):
    def __init__(self, message: str, *, status_code: Optional[int] = None, raw_metadata: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.raw_metadata = raw_metadata or {}


def resolve_api_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value.startswith("env:"):
        return os.getenv(value.removeprefix("env:").strip())
    return value


def resolve_model_config(
    model_config: Optional[ModelConfig] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> ResolvedModelConfig:
    payload = overrides or {}
    provider = payload.get("provider") or (model_config.provider if model_config else None) or settings.default_llm_provider
    model_name = payload.get("model_name") or (model_config.model_name if model_config else None) or settings.default_llm_model_name or "agentforge-debug-model"
    endpoint = payload.get("endpoint") or (model_config.endpoint if model_config else None) or settings.default_llm_base_url
    api_key_ref = payload.get("api_key_ref") or (model_config.api_key_ref if model_config else None) or settings.default_llm_api_key
    temperature = payload.get("temperature")
    if temperature is None:
        temperature = model_config.temperature if model_config else settings.default_llm_temperature
    max_tokens = payload.get("max_tokens")
    if max_tokens is None:
        max_tokens = model_config.max_tokens if model_config else settings.default_llm_max_tokens

    source = "inline-overrides"
    if not payload and model_config is not None:
        source = f"model-config:{model_config.id}"
    elif not payload and model_config is None:
        source = "env-defaults"
    elif payload and model_config is not None:
        source = f"model-config:{model_config.id}+inline-overrides"

    return ResolvedModelConfig(
        provider=str(provider),
        model_name=str(model_name),
        endpoint=endpoint.strip() if isinstance(endpoint, str) and endpoint.strip() else None,
        api_key=resolve_api_key(api_key_ref),
        temperature=float(temperature) if temperature is not None else None,
        max_tokens=int(max_tokens) if max_tokens is not None else None,
        source=source,
        llm_mode=settings.llm_mode,
    )


class BaseProviderAdapter:
    provider_key: str

    def __init__(self, runtime_config: ResolvedModelConfig) -> None:
        self.runtime_config = runtime_config

    def chat(self, messages: list[dict[str, str]]) -> ProviderResponse:
        prompt = "\n".join(f"{message['role']}: {message['content']}" for message in messages)
        return self.generate(prompt)

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ProviderToolResponse:
        """Chat with tool-use support. Returns ProviderToolResponse with single-intent contract.

        Default implementation uses prompt-based fallback: tool definitions are injected
        into the system prompt, and the LLM's text response is parsed for JSON action blocks.
        Subclasses may override with native function-calling support.
        """
        if self.runtime_config.llm_mode == "mock":
            return self._mock_tool_response(messages, tools)
        return self._prompt_based_tool_call(messages, tools)

    def generate(self, prompt: str) -> ProviderResponse:
        raise NotImplementedError

    def _mock_tool_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ProviderToolResponse:
        """Mock mode: always returns a final answer (no tool calls)."""
        tool_names = [t.get("function", {}).get("name", "unknown") for t in tools if isinstance(t, dict)]
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        content = f"[mock:tool_response] Available tools: {tool_names}. Task: {str(last_user)[:300]}"
        prompt_tokens = max(16, sum(len(str(m.get("content", ""))) for m in messages) // 4)
        completion_tokens = max(24, len(content) // 4)
        return ProviderToolResponse(
            content=content,
            tool_calls=[],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            raw_metadata={"mode": "mock", "provider": self.provider_key},
            finish_reason="stop",
        )

    def _prompt_based_tool_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ProviderToolResponse:
        """Fallback for providers without native function-calling.

        Injects tool descriptions into the system prompt and parses
        JSON action blocks from the LLM's text response.
        """
        tool_descriptions = self._format_tools_for_prompt(tools)
        augmented_messages = list(messages)
        system_injection = (
            "You have access to the following tools. To call a tool, respond with EXACTLY "
            "one JSON block in this format:\n"
            '```json\n{"tool_call": {"name": "<tool_name>", "arguments": {<args>}}}\n```\n'
            "To give your final answer (no more tool calls needed), respond with plain text "
            "WITHOUT any tool_call JSON block.\n\n"
            f"Available tools:\n{tool_descriptions}"
        )
        if augmented_messages and augmented_messages[0].get("role") == "system":
            augmented_messages[0] = {
                **augmented_messages[0],
                "content": augmented_messages[0].get("content", "") + "\n\n" + system_injection,
            }
        else:
            augmented_messages.insert(0, {"role": "system", "content": system_injection})

        flat_prompt = "\n".join(
            f"{m['role']}: {m.get('content', '')}" for m in augmented_messages
        )
        response = self.generate(flat_prompt)
        parsed_tool_call = self._parse_tool_call_from_text(response.content)
        if parsed_tool_call is not None:
            return ProviderToolResponse(
                content=None,
                tool_calls=[parsed_tool_call],
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens,
                raw_metadata=response.raw_metadata,
                finish_reason="tool_calls",
            )
        return ProviderToolResponse(
            content=response.content,
            tool_calls=[],
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            raw_metadata=response.raw_metadata,
            finish_reason="stop",
        )

    def _format_tools_for_prompt(self, tools: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for tool in tools:
            func = tool.get("function", {}) if isinstance(tool, dict) else {}
            name = func.get("name", "unknown")
            desc = func.get("description", "")
            params = func.get("parameters", {})
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            param_desc = ", ".join(
                f"{k}: {v.get('type', 'any')}" for k, v in props.items()
            ) if isinstance(props, dict) else ""
            lines.append(f"- {name}({param_desc}): {desc}")
        return "\n".join(lines)

    @staticmethod
    def _parse_tool_call_from_text(text: str) -> Optional[ToolCall]:
        """Parse a tool_call JSON block from LLM text output.

        Looks for ```json\n{...}\n``` fenced blocks first, then bare {"tool_call": ...} objects.
        Uses brace-counting for bare JSON to handle nested objects.
        Returns None if no valid tool call found (meaning it's a final answer).
        """
        import re
        fenced = re.search(r'```(?:json)?\s*(\{[^`]*?"tool_call"[^`]*?\})\s*```', text, re.DOTALL)
        if fenced:
            candidate = fenced.group(1).strip()
            result = BaseProviderAdapter._try_parse_tool_call_json(candidate)
            if result is not None:
                return result
        marker = '"tool_call"'
        idx = text.find(marker)
        while idx >= 0:
            brace_start = text.rfind("{", 0, idx)
            if brace_start >= 0:
                candidate = BaseProviderAdapter._extract_balanced_braces(text, brace_start)
                if candidate is not None:
                    result = BaseProviderAdapter._try_parse_tool_call_json(candidate)
                    if result is not None:
                        return result
            idx = text.find(marker, idx + len(marker))
        return None

    @staticmethod
    def _extract_balanced_braces(text: str, start: int) -> Optional[str]:
        """Extract a balanced {} block starting at `start`. Returns None if unbalanced."""
        if start >= len(text) or text[start] != "{":
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None

    @staticmethod
    def _try_parse_tool_call_json(candidate: str) -> Optional[ToolCall]:
        try:
            parsed = json.loads(candidate)
            call = parsed.get("tool_call", {})
            name = str(call.get("name", "")).strip()
            if name:
                return ToolCall(
                    tool_call_id=f"parsed_{name}_{id(candidate) % 10000}",
                    tool_name=name,
                    tool_args=call.get("arguments", {}) if isinstance(call.get("arguments"), dict) else {},
                )
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        return None

    def _mock_response(self, prompt: str, source: str) -> ProviderResponse:
        content = f"[mock:{source}] {prompt[:400]}"
        prompt_tokens = max(16, len(prompt) // 4)
        completion_tokens = max(24, len(content) // 4)
        total_tokens = prompt_tokens + completion_tokens
        return ProviderResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            raw_metadata={
                "mode": "mock",
                "provider": self.provider_key,
                "source": source,
                "model_name": self.runtime_config.model_name,
            },
        )

    def _require_real_mode_fields(self, *, require_api_key: bool) -> None:
        if not self.runtime_config.endpoint:
            raise ProviderCallError("Missing provider endpoint configuration.")
        if not self.runtime_config.model_name:
            raise ProviderCallError("Missing model name configuration.")
        if require_api_key and not self.runtime_config.api_key:
            raise ProviderCallError("Missing API key configuration.")

    def _request(self, request: Request, *, request_kind: str) -> dict[str, Any]:
        start = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "provider.request.started",
            provider=self.provider_key,
            mode=self.runtime_config.llm_mode,
            source=self.runtime_config.source,
            request_kind=request_kind,
            endpoint=request.full_url,
            model_name=self.runtime_config.model_name,
            api_key=self.runtime_config.api_key,
        )
        try:
            with urlopen(request, timeout=settings.llm_request_timeout_seconds) as response:
                status_code = getattr(response, "status", None)
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            try:
                error_body = exc.read().decode("utf-8")
            except Exception:
                error_body = exc.reason if hasattr(exc, "reason") else str(exc)
            log_event(
                logger,
                logging.ERROR,
                "provider.request.failed",
                provider=self.provider_key,
                source=self.runtime_config.source,
                request_kind=request_kind,
                endpoint=request.full_url,
                status_code=exc.code,
                latency_ms=latency_ms,
                error=error_body,
            )
            raise ProviderCallError(
                f"Provider returned HTTP {exc.code}",
                status_code=exc.code,
                raw_metadata={"error_body": error_body},
            ) from exc
        except (URLError, OSError, ValueError, IncompleteRead) as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            log_event(
                logger,
                logging.ERROR,
                "provider.request.failed",
                provider=self.provider_key,
                source=self.runtime_config.source,
                request_kind=request_kind,
                endpoint=request.full_url,
                latency_ms=latency_ms,
                error=str(exc),
            )
            raise ProviderCallError(
                "Provider request failed before receiving a valid response.",
                raw_metadata={"error": str(exc)},
            ) from exc

        latency_ms = int((time.perf_counter() - start) * 1000)
        log_event(
            logger,
            logging.INFO,
            "provider.request.completed",
            provider=self.provider_key,
            source=self.runtime_config.source,
            request_kind=request_kind,
            endpoint=request.full_url,
            status_code=status_code,
            latency_ms=latency_ms,
        )
        return body


class OpenAICompatibleProviderAdapter(BaseProviderAdapter):
    provider_key = ModelProviderType.OPENAI_COMPATIBLE.value

    def generate(self, prompt: str) -> ProviderResponse:
        if self.runtime_config.llm_mode == "mock":
            return self._mock_response(prompt, "openai-compatible-mock")

        self._require_real_mode_fields(require_api_key=True)
        payload = {
            "model": self.runtime_config.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.runtime_config.temperature,
            "max_tokens": self.runtime_config.max_tokens,
        }
        endpoint = self._resolve_chat_endpoint(self.runtime_config.endpoint or "")
        request = Request(
            endpoint,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.runtime_config.api_key}",
            },
        )
        body = self._request(request, request_kind="chat.completions")
        choice = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        usage = body.get("usage") or {}
        return ProviderResponse(
            content=choice,
            prompt_tokens=int(usage.get("prompt_tokens", max(16, len(prompt) // 4))),
            completion_tokens=int(usage.get("completion_tokens", max(24, len(choice) // 4))),
            total_tokens=int(usage.get("total_tokens", max(40, (len(prompt) + len(choice)) // 4))),
            raw_metadata={"mode": "real", "provider": self.provider_key, **body},
        )

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ProviderToolResponse:
        if self.runtime_config.llm_mode == "mock":
            return self._mock_tool_response(messages, tools)

        self._require_real_mode_fields(require_api_key=True)
        payload: dict[str, Any] = {
            "model": self.runtime_config.model_name,
            "messages": messages,
            "temperature": self.runtime_config.temperature,
            "max_tokens": self.runtime_config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
        endpoint = self._resolve_chat_endpoint(self.runtime_config.endpoint or "")
        request = Request(
            endpoint,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.runtime_config.api_key}",
            },
        )
        body = self._request(request, request_kind="chat.completions.tools")
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = str(choice.get("finish_reason") or "stop")
        content = message.get("content")
        raw_tool_calls = message.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
        for raw_tc in raw_tool_calls:
            if not isinstance(raw_tc, dict):
                continue
            func = raw_tc.get("function") or {}
            name = str(func.get("name") or "").strip()
            if not name:
                continue
            try:
                args = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_calls.append(ToolCall(
                tool_call_id=str(raw_tc.get("id") or f"tc_{name}"),
                tool_name=name,
                tool_args=args if isinstance(args, dict) else {},
            ))
        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens))
        return ProviderToolResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            raw_metadata={"mode": "real", "provider": self.provider_key, **body},
            finish_reason="tool_calls" if tool_calls else finish_reason,
        )

    def _resolve_chat_endpoint(self, endpoint: str) -> str:
        trimmed = endpoint.rstrip("/")
        if trimmed.endswith("/chat/completions"):
            return trimmed
        return trimmed + "/chat/completions"


class OpenAIProviderAdapter(OpenAICompatibleProviderAdapter):
    provider_key = ModelProviderType.OPENAI.value


class LocalEndpointProviderAdapter(BaseProviderAdapter):
    provider_key = ModelProviderType.LOCAL_ENDPOINT.value

    def generate(self, prompt: str) -> ProviderResponse:
        if self.runtime_config.llm_mode == "mock":
            return self._mock_response(prompt, "local-endpoint-mock")

        self._require_real_mode_fields(require_api_key=False)
        payload = {
            "model": self.runtime_config.model_name,
            "prompt": prompt,
            "temperature": self.runtime_config.temperature,
            "max_tokens": self.runtime_config.max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if self.runtime_config.api_key:
            headers["Authorization"] = f"Bearer {self.runtime_config.api_key}"
        request = Request(
            self.runtime_config.endpoint or "",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        body = self._request(request, request_kind="generate")
        content = body.get("content") or body.get("response") or body.get("text") or ""
        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", max(16, len(prompt) // 4)))
        completion_tokens = int(usage.get("completion_tokens", max(24, len(content) // 4)))
        return ProviderResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=int(usage.get("total_tokens", prompt_tokens + completion_tokens)),
            raw_metadata={"mode": "real", "provider": self.provider_key, **body},
        )


def get_provider_adapter(
    model_config: Optional[ModelConfig] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> Tuple[BaseProviderAdapter, ResolvedModelConfig]:
    runtime_config = resolve_model_config(model_config, overrides=overrides)
    if runtime_config.provider == ModelProviderType.LOCAL_ENDPOINT.value:
        return LocalEndpointProviderAdapter(runtime_config), runtime_config
    if runtime_config.provider == ModelProviderType.OPENAI.value:
        return OpenAIProviderAdapter(runtime_config), runtime_config
    return OpenAICompatibleProviderAdapter(runtime_config), runtime_config
