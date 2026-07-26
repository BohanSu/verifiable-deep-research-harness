from __future__ import annotations

from typing import Any

from ..config import MODEL_AGENT_ROLES
from .base import ModelProvider


class ModelProviderTeam:
    """A role-routed model roster with aggregate, per-model usage evidence."""

    def __init__(
        self,
        providers: dict[str, ModelProvider],
        routes: dict[str, str],
        *,
        profile: str,
    ) -> None:
        missing_roles = set(MODEL_AGENT_ROLES) - set(routes)
        if missing_roles:
            raise ValueError(
                "model team is missing role routes: " + ", ".join(sorted(missing_roles))
            )
        missing_models = set(routes.values()) - set(providers)
        if missing_models:
            raise ValueError(
                "model team is missing providers: " + ", ".join(sorted(missing_models))
            )
        self.providers = dict(providers)
        self.routes = {role: routes[role] for role in MODEL_AGENT_ROLES}
        self.profile = profile
        self.model_choice = profile
        self.model = "+".join(sorted(set(self.routes.values())))
        first = next(iter(self.providers.values()))
        self.base_url = str(getattr(first, "base_url", "local"))

    def provider_for(self, role: str) -> ModelProvider:
        if role not in self.routes:
            raise ValueError(f"unknown model role: {role}")
        return self.providers[self.routes[role]]

    def identity_for(self, role: str) -> dict[str, object]:
        provider = self.provider_for(role)
        return {
            "role": role,
            "choice": self.routes[role],
            "provider": type(provider).__name__,
            "model": str(getattr(provider, "model", "built-in")),
            "base_url": str(getattr(provider, "base_url", "local")),
            "modalities": list(getattr(provider, "modalities", ("text", "document"))),
        }

    def route_snapshot(self) -> dict[str, dict[str, object]]:
        return {role: self.identity_for(role) for role in MODEL_AGENT_ROLES}

    def usage_snapshot(self) -> dict[str, Any]:
        totals: dict[str, Any] = {
            "model_calls": 0,
            "model_cache_hits": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
            "provider": "ModelProviderTeam",
        }
        by_model: dict[str, object] = {}
        pricing_statuses: list[str] = []
        pricing_reasons: list[str] = []
        seen: set[int] = set()
        for choice, provider in self.providers.items():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            snapshot_method = getattr(provider, "usage_snapshot", None)
            snapshot = snapshot_method() if callable(snapshot_method) else {}
            normalized: dict[str, Any] = {
                "model_calls": int(snapshot.get("model_calls", 0)),
                "model_cache_hits": int(snapshot.get("model_cache_hits", 0)),
                "input_tokens": int(snapshot.get("input_tokens", 0)),
                "output_tokens": int(snapshot.get("output_tokens", 0)),
                "estimated_cost_usd": float(snapshot.get("estimated_cost_usd", 0.0)),
                "provider": str(snapshot.get("provider") or type(provider).__name__),
                "model": str(getattr(provider, "model", "built-in")),
                "pricing_configured": bool(snapshot.get("pricing_configured", False)),
                "pricing_status": str(snapshot.get("pricing_status") or "unavailable"),
                "pricing_reason": str(snapshot.get("pricing_reason") or ""),
            }
            by_model[choice] = normalized
            for key in (
                "model_calls",
                "model_cache_hits",
                "input_tokens",
                "output_tokens",
                "estimated_cost_usd",
            ):
                totals[key] += normalized[key]
            if normalized["model_calls"] or normalized["model_cache_hits"]:
                pricing_statuses.append(str(normalized["pricing_status"]))
                reason = str(normalized["pricing_reason"])
                if reason:
                    pricing_reasons.append(reason)
        totals["by_model"] = by_model
        totals["pricing_configured"] = bool(pricing_statuses) and all(
            bool(item.get("pricing_configured"))
            for item in by_model.values()
        )
        if not pricing_statuses:
            totals["pricing_status"] = "unavailable"
        elif all(item == "complete" for item in pricing_statuses):
            totals["pricing_status"] = "complete"
        elif all(item == "unavailable" for item in pricing_statuses):
            totals["pricing_status"] = "unavailable"
        else:
            totals["pricing_status"] = "partial"
        totals["pricing_reason"] = " ".join(dict.fromkeys(pricing_reasons))
        return totals
