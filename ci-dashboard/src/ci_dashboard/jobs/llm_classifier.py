from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import httpx

from ci_dashboard.common.config import LLMSettings
from ci_dashboard.common.models import ErrorClassification

DEFAULT_LLM_TIMEOUT_SECONDS = 60
DEFAULT_LLM_MAX_INPUT_CHARS = 24000


class LLMClassifier(Protocol):
    def classify(self, *, log_text: str, build: Mapping[str, Any]) -> ErrorClassification:
        ...


@dataclass(frozen=True)
class NoopLLMClassifier:
    default_l1_category: str
    default_l2_subcategory: str

    def classify(self, *, log_text: str, build: Mapping[str, Any]) -> ErrorClassification:
        del log_text, build
        return ErrorClassification(
            l1_category=self.default_l1_category,
            l2_subcategory=self.default_l2_subcategory,
            source="llm:noop",
        )


@dataclass(frozen=True)
class OpenAICompatibleLLMClassifier:
    provider_name: str
    base_url: str
    model: str
    api_key: str
    default_l1_category: str
    default_l2_subcategory: str
    allowed_classifications: tuple[tuple[str, str], ...]
    timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS
    max_input_chars: int = DEFAULT_LLM_MAX_INPUT_CHARS
    transport: httpx.BaseTransport | None = None

    def classify(self, *, log_text: str, build: Mapping[str, Any]) -> ErrorClassification:
        response_payload = self._post_chat_completion(
            _build_chat_payload(
                model=self.model,
                allowed_classifications=self.allowed_classifications,
                default_l1_category=self.default_l1_category,
                default_l2_subcategory=self.default_l2_subcategory,
                log_text=_truncate_log_text(log_text, max_chars=self.max_input_chars),
                build=build,
            )
        )
        parsed = _parse_classification_response(response_payload)
        return _validate_classification(
            parsed,
            allowed_classifications=self.allowed_classifications,
            provider_name=self.provider_name,
        )

    def _post_chat_completion(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()


def build_llm_classifier(
    settings: LLMSettings,
    *,
    default_l1_category: str,
    default_l2_subcategory: str,
    allowed_classifications: tuple[tuple[str, str], ...],
) -> LLMClassifier:
    provider = (settings.provider or "noop").strip().lower()
    if provider in {"", "noop", "none"}:
        return NoopLLMClassifier(
            default_l1_category=default_l1_category,
            default_l2_subcategory=default_l2_subcategory,
        )
    if provider in {"codex", "openai-compatible", "openai_compatible"}:
        base_url = _require_setting(settings.base_url, name="CI_DASHBOARD_LLM_BASE_URL")
        model = _require_setting(settings.model, name="CI_DASHBOARD_LLM_MODEL")
        api_key = _require_setting(settings.api_key, name="CI_DASHBOARD_LLM_API_KEY")
        return OpenAICompatibleLLMClassifier(
            provider_name=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            default_l1_category=default_l1_category,
            default_l2_subcategory=default_l2_subcategory,
            allowed_classifications=allowed_classifications,
        )
    raise ValueError(
        f"unsupported CI_DASHBOARD_LLM_PROVIDER {settings.provider!r}; "
        "supported values are 'noop' and 'codex'"
    )


def _require_setting(value: str | None, *, name: str) -> str:
    resolved = (value or "").strip()
    if not resolved:
        raise ValueError(f"{name} is required when CI_DASHBOARD_LLM_PROVIDER is enabled")
    return resolved


def _truncate_log_text(log_text: str, *, max_chars: int) -> str:
    if len(log_text) <= max_chars:
        return log_text
    return f"[TRUNCATED TO LAST {max_chars} CHARS]\n{log_text[-max_chars:]}"


def _build_chat_payload(
    *,
    model: str,
    allowed_classifications: tuple[tuple[str, str], ...],
    default_l1_category: str,
    default_l2_subcategory: str,
    log_text: str,
    build: Mapping[str, Any],
) -> dict[str, Any]:
    allowed_lines = "\n".join(
        f"- {l1_category}/{l2_subcategory}" for l1_category, l2_subcategory in allowed_classifications
    )
    job_name = str(build.get("job_name") or "")
    url = str(build.get("url") or "")
    system_prompt = (
        "You classify Jenkins CI build failures into a fixed taxonomy. "
        "Return JSON only with keys 'l1' and 'l2'. "
        f"If the evidence is weak, return {default_l1_category}/{default_l2_subcategory}. "
        "Do not invent new categories."
    )
    user_prompt = (
        "The deterministic rule engine already found no exact rule match.\n"
        "Choose the best category from the allowed list below.\n\n"
        "Allowed categories:\n"
        f"{allowed_lines}\n\n"
        "Build context:\n"
        f"- job_name: {job_name or '<unknown>'}\n"
        f"- url: {url or '<unknown>'}\n\n"
        "Redacted console log tail:\n"
        f"{log_text}\n"
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }


def _parse_classification_response(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response did not contain choices")
    first_choice = choices[0]
    if not isinstance(first_choice, Mapping):
        raise ValueError("LLM response choice is malformed")
    message = first_choice.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("LLM response message is missing")
    content = _coerce_message_content(message.get("content"))
    return _extract_json_object(content)


def _coerce_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text_value = item.get("text")
                if text_value:
                    parts.append(str(text_value))
        if parts:
            return "\n".join(parts)
    raise ValueError("LLM response content is empty")


def _extract_json_object(content: str) -> Mapping[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")
    candidate = stripped[start : end + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, Mapping):
        raise ValueError("LLM response JSON must be an object")
    return parsed


def _validate_classification(
    payload: Mapping[str, Any],
    *,
    allowed_classifications: tuple[tuple[str, str], ...],
    provider_name: str,
) -> ErrorClassification:
    l1_category = str(payload.get("l1") or "").strip().upper()
    l2_subcategory = str(payload.get("l2") or "").strip().upper()
    candidate = (l1_category, l2_subcategory)
    if candidate not in set(allowed_classifications):
        raise ValueError(f"LLM returned unsupported classification {candidate!r}")
    return ErrorClassification(
        l1_category=l1_category,
        l2_subcategory=l2_subcategory,
        source=f"llm:{provider_name}",
    )
