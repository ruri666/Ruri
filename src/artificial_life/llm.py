"""Provider-neutral LLM ports and the local/DeepSeek implementations."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


DEFAULT_DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


@dataclass(frozen=True)
class LLMResult:
    text: str
    structured: dict[str, Any] = field(default_factory=dict)
    provider: str = "mock"
    purpose: str = ""


class LLMProvider(Protocol):
    def generate(self, prompt: str, *, purpose: str = "generation", context: dict[str, Any] | None = None) -> LLMResult: ...

    def generate_stream(
        self,
        prompt: str,
        *,
        purpose: str = "generation",
        context: dict[str, Any] | None = None,
    ) -> Iterator[str]: ...

    def summarize(
        self,
        source_text: str,
        *,
        purpose: str,
        source_refs: list[dict[str, Any]],
        scale: str = "event",
    ) -> LLMResult: ...

    def plan(self, context: dict[str, Any], candidates: list[dict[str, Any]]) -> LLMResult: ...


class MockLLMProvider:
    """Deterministic provider used by V1 and tests; no network calls."""

    provider_name = "mock"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt: str, *, purpose: str = "generation", context: dict[str, Any] | None = None) -> LLMResult:
        self.calls.append({"method": "generate", "purpose": purpose, "prompt": prompt, "context": context or {}})
        return LLMResult(text=prompt.strip(), provider="mock", purpose=purpose)

    def generate_stream(
        self,
        prompt: str,
        *,
        purpose: str = "generation",
        context: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        result = self.generate(prompt, purpose=purpose, context=context)
        yield from _text_chunks(result.text)

    def summarize(
        self,
        source_text: str,
        *,
        purpose: str,
        source_refs: list[dict[str, Any]],
        scale: str = "event",
    ) -> LLMResult:
        self.calls.append(
            {
                "method": "summarize",
                "purpose": purpose,
                "source_text": source_text,
                "source_refs": source_refs,
                "scale": scale,
            }
        )
        compact = " ".join(source_text.split())
        if len(compact) > 500:
            compact = compact[:497] + "..."
        prefix = {"event": "事件摘要", "day": "当日摘要", "week": "本周摘要", "month": "本月摘要", "year": "年度摘要"}.get(scale, "摘要")
        return LLMResult(text=f"{prefix}：{compact}" if compact else f"{prefix}：暂无可总结内容", provider="mock", purpose=purpose)

    def plan(self, context: dict[str, Any], candidates: list[dict[str, Any]]) -> LLMResult:
        self.calls.append({"method": "plan", "context": context, "candidates": candidates})
        selected = candidates[0] if candidates else {"action": "reserved_free"}
        return LLMResult(text=str(selected), structured={"selected": selected}, provider="mock", purpose="planning")


class DeepSeekProvider:
    """DeepSeek chat-completions adapter for dialogue, planning, and summaries."""

    provider_name = "deepseek"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout: float = 60.0,
        max_tokens: int = 1024,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek API key cannot be empty")
        if not base_url.strip():
            raise ValueError("DeepSeek base URL cannot be empty")
        if timeout <= 0:
            raise ValueError("DeepSeek timeout must be positive")
        if max_tokens <= 0:
            raise ValueError("DeepSeek max_tokens must be positive")
        self.api_key = api_key.strip()
        try:
            self.api_key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("DeepSeek API key must contain ASCII characters only") from exc
        self.base_url = base_url.rstrip("/")
        self.model = model or DEFAULT_DEEPSEEK_MODEL
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_env(cls) -> "DeepSeekProvider":
        """Build a provider from environment variables without persisting secrets."""

        api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        model = os.getenv("DEEPSEEK_MODEL") or os.getenv("OPENAI_MODEL", DEFAULT_DEEPSEEK_MODEL)
        base_url = os.getenv("DEEPSEEK_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
        timeout = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "60"))
        max_tokens = int(os.getenv("DEEPSEEK_MAX_TOKENS", "1024"))
        return cls(api_key, base_url=base_url, model=model, timeout=timeout, max_tokens=max_tokens)

    def generate(
        self,
        prompt: str,
        *,
        purpose: str = "generation",
        context: dict[str, Any] | None = None,
    ) -> LLMResult:
        system = (
            "你是一个持久化人工生命 Agent 的对话回复模型。"
            "当前人设只以提供的核心上下文为准，不从历史内容推断或混入其他身份。请自然、直接地回应用户，"
            "结合提供的上下文，但不要提及 provider、prompt、内部实现或密钥；"
            "不要把计划、推测或摘要当成已经发生的事实。"
        )
        if context:
            system += "\n以下是仅供你参考的内部上下文 JSON：\n" + json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "thinking": {"type": "disabled"},
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        self.calls.append({"method": "generate", "purpose": purpose, "model": self.model})
        text = self._complete(payload)
        return LLMResult(text=text, provider="deepseek", purpose=purpose)

    def generate_stream(
        self,
        prompt: str,
        *,
        purpose: str = "generation",
        context: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        system = (
            "你是一个持久化人工生命 Agent 的对话回复模型。"
            "当前人设只以提供的核心上下文为准，不从历史内容推断或混入其他身份。请自然、直接地回应用户，"
            "结合提供的上下文，但不要提及 provider、prompt、内部实现或密钥；"
            "不要把计划、推测或摘要当成已经发生的事实。"
        )
        if context:
            system += "\n以下是仅供你参考的内部上下文 JSON：\n" + json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "thinking": {"type": "disabled"},
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        self.calls.append({"method": "generate_stream", "purpose": purpose, "model": self.model})
        yield from self._stream_complete(payload)

    def summarize(
        self,
        source_text: str,
        *,
        purpose: str,
        source_refs: list[dict[str, Any]],
        scale: str = "event",
    ) -> LLMResult:
        system = (
            "你是记忆摘要模型。请把给定的原始事实压缩成简洁、保守、可核验的中文摘要。"
            "只能使用输入事实，不得补写未出现的事实；只返回摘要正文，不要返回 JSON 或解释。"
        )
        prompt = json.dumps(
            {"scale": scale, "source_refs": source_refs, "source_text": source_text},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload = self._payload(system, prompt, purpose=purpose, stream=False)
        self.calls.append({"method": "summarize", "purpose": purpose, "model": self.model})
        text = self._complete(payload)
        return LLMResult(text=text, provider="deepseek", purpose=purpose)

    def plan(self, context: dict[str, Any], candidates: list[dict[str, Any]]) -> LLMResult:
        system = (
            "你是持续生活规划器。请从候选行为中选择下一步行为，或选择 reserved_free。"
            "只返回一个 JSON 对象，不要 Markdown，不要解释。对象必须包含 action；"
            "已有活动只能使用 start_activity 或 resume_activity，不能创造候选之外的 activity_id。"
            "允许的 action: start_activity, resume_activity, create_activity, reserved_free。"
        )
        prompt = json.dumps(
            {"context": context, "candidates": candidates},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload = self._payload(system, prompt, purpose="planning", stream=False)
        self.calls.append({"method": "plan", "purpose": "planning", "model": self.model})
        text = self._complete(payload)
        structured = _parse_json_object(text) or {}
        return LLMResult(text=text, structured=structured, provider="deepseek", purpose="planning")

    def _payload(self, system: str, prompt: str, *, purpose: str, stream: bool) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "thinking": {"type": "disabled"},
            "max_tokens": self.max_tokens,
            "stream": stream,
        }

    def _complete(self, payload: dict[str, Any]) -> str:
        return chat_completion(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout,
                               payload=payload, provider_name="DeepSeek")

    def _stream_complete(self, payload: dict[str, Any]) -> Iterator[str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if data_text == "[DONE]":
                        return
                    try:
                        data = json.loads(data_text)
                        content = data["choices"][0].get("delta", {}).get("content", "")
                    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                        raise RuntimeError("DeepSeek API returned an invalid stream event") from exc
                    if isinstance(content, str) and content:
                        yield content
        except HTTPError as exc:
            detail = _response_error(exc.read())
            raise RuntimeError(f"DeepSeek API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"DeepSeek API connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("DeepSeek API request timed out") from exc


class OllamaProvider:
    """Local Ollama chat adapter.

    Ollama is intentionally optional and accessed through its HTTP API using
    only the standard library. The adapter never writes canonical history; it
    only returns candidate natural-language/semantic results. It is retained
    for local experiments while the current production configuration uses
    DeepSeek for every LLM purpose.
    """

    provider_name = "ollama"

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        *,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 120.0,
        max_tokens: int = 1024,
    ) -> None:
        if not model.strip():
            raise ValueError("Ollama model cannot be empty")
        if not base_url.strip():
            raise ValueError("Ollama base URL cannot be empty")
        if timeout <= 0:
            raise ValueError("Ollama timeout must be positive")
        if max_tokens <= 0:
            raise ValueError("Ollama max_tokens must be positive")
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.calls: list[dict[str, Any]] = []
        self._deterministic = MockLLMProvider()

    @classmethod
    def from_env(cls) -> "OllamaProvider":
        return cls(
            model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120")),
            max_tokens=int(os.getenv("OLLAMA_MAX_TOKENS", "1024")),
        )

    def generate(
        self,
        prompt: str,
        *,
        purpose: str = "generation",
        context: dict[str, Any] | None = None,
    ) -> LLMResult:
        system = (
            "你是一个持久化人工生命 Agent。当前人设只以提供的核心上下文为准，不从历史内容推断或混入其他身份。"
            "请自然、直接地回应用户，结合上下文，但不要提及 provider、prompt、"
            "内部实现或密钥；不要把计划、推测或摘要当成已经发生的事实。"
        )
        if context:
            system += "\n以下 JSON 是只读上下文：\n" + json.dumps(
                context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"num_predict": self.max_tokens},
        }
        self.calls.append({"method": "generate", "purpose": purpose, "model": self.model})
        text = self._complete(payload)
        return LLMResult(text=text, provider="ollama", purpose=purpose)

    def generate_stream(
        self,
        prompt: str,
        *,
        purpose: str = "generation",
        context: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        system = (
            "你是一个持久化人工生命 Agent。当前人设只以提供的核心上下文为准，不从历史内容推断或混入其他身份。"
            "请自然、直接地回应用户，结合上下文，但不要提及 provider、prompt、"
            "内部实现或密钥；不要把计划、推测或摘要当成已经发生的事实。"
        )
        if context:
            system += "\n以下 JSON 是只读上下文：\n" + json.dumps(
                context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "options": {"num_predict": self.max_tokens},
        }
        self.calls.append({"method": "generate_stream", "purpose": purpose, "model": self.model})
        yield from self._stream_complete(payload)

    def summarize(
        self,
        source_text: str,
        *,
        purpose: str,
        source_refs: list[dict[str, Any]],
        scale: str = "event",
    ) -> LLMResult:
        return self._deterministic.summarize(source_text, purpose=purpose, source_refs=source_refs, scale=scale)

    def plan(self, context: dict[str, Any], candidates: list[dict[str, Any]]) -> LLMResult:
        return self._deterministic.plan(context, candidates)

    def _complete(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = _response_error(exc.read())
            raise RuntimeError(f"Ollama API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            reason = exc.reason or "server unavailable"
            raise RuntimeError(
                f"Ollama API connection failed: {reason}. Start Ollama and pull {self.model!r}."
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError("Ollama API request timed out") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
            content = data["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError("Ollama API returned an invalid chat response") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Ollama API returned an empty response")
        return content.strip()

    def _stream_complete(self, payload: dict[str, Any]) -> Iterator[str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Accept": "application/x-ndjson", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    try:
                        data = json.loads(raw_line.decode("utf-8"))
                        content = data.get("message", {}).get("content", "")
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
                        raise RuntimeError("Ollama API returned an invalid stream event") from exc
                    if isinstance(content, str) and content:
                        yield content
                    if data.get("done") is True:
                        return
        except HTTPError as exc:
            detail = _response_error(exc.read())
            raise RuntimeError(f"Ollama API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            reason = exc.reason or "server unavailable"
            raise RuntimeError(
                f"Ollama API connection failed: {reason}. Start Ollama and pull {self.model!r}."
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError("Ollama API request timed out") from exc


def chat_completion(*, base_url: str, api_key: str, timeout: float, payload: dict,
                    provider_name: str = "Model", use_env_proxy: bool = True) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        open_request = urlopen if use_env_proxy else build_opener(ProxyHandler({})).open
        with open_request(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = _response_error(exc.read())
        raise RuntimeError(f"{provider_name} API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{provider_name} API connection failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"{provider_name} API request timed out") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{provider_name} API returned an invalid chat-completion response") from exc
    if data["choices"][0].get("finish_reason") == "length":
        raise RuntimeError(f"{provider_name} API response was truncated; increase max_tokens")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"{provider_name} API returned an empty response")
    return content.strip()


def default_provider() -> LLMProvider:
    """Select the configured provider, preferring DeepSeek when configured.

    An explicit ``AGENT_LLM_PROVIDER`` still wins. When it is omitted, a
    configured DeepSeek key selects the current production provider; otherwise
    the offline Mock keeps local tests and first-run initialization safe.
    """

    configured_name = os.getenv("AGENT_LLM_PROVIDER")
    provider_name = (
        configured_name.strip().lower()
        if configured_name is not None
        else ("deepseek" if (os.getenv("DEEPSEEK_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()) else "mock")
    )
    if provider_name in {"deepseek", "openai"}:
        return DeepSeekProvider.from_env()
    if provider_name in {"ollama", "local"}:
        return OllamaProvider.from_env()
    if provider_name not in {"", "mock"}:
        raise ValueError(f"unsupported AGENT_LLM_PROVIDER: {provider_name}")
    return MockLLMProvider()


def _response_error(raw: bytes) -> str:
    """Extract a short provider error without ever echoing request headers."""

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unreadable error response"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if data.get("message"):
            return str(data["message"])
    return "request rejected"


def _parse_json_object(text: str) -> dict[str, Any] | None:
    value = text.strip()
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.startswith("json"):
            value = value[4:].strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _text_chunks(text: str, size: int = 24) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index : index + size]
