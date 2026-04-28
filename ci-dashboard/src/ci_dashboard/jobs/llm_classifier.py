from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ci_dashboard.common.config import LLMSettings
from ci_dashboard.common.models import ErrorClassification


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


def build_llm_classifier(
    settings: LLMSettings,
    *,
    default_l1_category: str,
    default_l2_subcategory: str,
) -> LLMClassifier:
    provider = (settings.provider or "noop").strip().lower()
    if provider in {"", "noop", "none"}:
        return NoopLLMClassifier(
            default_l1_category=default_l1_category,
            default_l2_subcategory=default_l2_subcategory,
        )
    raise ValueError(
        f"unsupported CI_DASHBOARD_LLM_PROVIDER {settings.provider!r}; "
        "only 'noop' is implemented in the current slice"
    )
