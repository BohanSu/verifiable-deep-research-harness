from __future__ import annotations

from ..cache import FileCache
from ..config import AppConfig, normalize_model_profile
from .base import ModelProvider, SearchProvider
from .deepseek import OpenAICompatibleModelProvider
from .mock import MockModelProvider, ReplaySearchProvider
from .team import ModelProviderTeam
from .web import BraveSearchProvider, DuckDuckGoSearchProvider, OpenAlexSearchProvider


def build_providers(
    config: AppConfig,
    model_choice: object | None = None,
) -> tuple[ModelProvider, SearchProvider]:
    cache = FileCache(config.cache_dir)
    if config.model_provider in {"openai_compatible", "deepseek"}:
        selected = config.select_model(model_choice)
        model: ModelProvider = _build_online_model(config, cache, selected)
    elif config.model_provider == "mock":
        model = MockModelProvider()
    else:
        raise ValueError(f"Unknown model provider: {config.model_provider}")

    return model, build_search_provider(config, cache)


def build_model_team(
    config: AppConfig,
    profile: object | None = None,
) -> tuple[ModelProviderTeam, SearchProvider]:
    selected_profile = normalize_model_profile(
        config.model_profile if profile is None else profile
    )
    routes = config.profile_routes(selected_profile)
    cache = FileCache(config.cache_dir)
    if config.model_provider in {"openai_compatible", "deepseek"}:
        providers: dict[str, ModelProvider] = {}
        for choice in sorted(set(routes.values())):
            providers[choice] = _build_online_model(config, cache, choice)
    elif config.model_provider == "mock":
        mock = MockModelProvider()
        providers = {choice: mock for choice in set(routes.values())}
    else:
        raise ValueError(f"Unknown model provider: {config.model_provider}")

    return ModelProviderTeam(
        providers,
        routes,
        profile=selected_profile,
    ), build_search_provider(config, cache)


def build_search_provider(
    config: AppConfig,
    cache: FileCache | None = None,
) -> SearchProvider:
    provider = config.require_search_provider()
    if provider == "duckduckgo":
        return DuckDuckGoSearchProvider(
            cache or FileCache(config.cache_dir),
            allow_rfc2544_proxy_fake_ip=config.allow_rfc2544_proxy_fake_ip,
        )
    if provider == "openalex":
        return OpenAlexSearchProvider(
            cache or FileCache(config.cache_dir),
            allow_rfc2544_proxy_fake_ip=config.allow_rfc2544_proxy_fake_ip,
        )
    if provider == "brave":
        return BraveSearchProvider(
            cache or FileCache(config.cache_dir),
            api_key=config.resolved_brave_api_key,
            allow_rfc2544_proxy_fake_ip=config.allow_rfc2544_proxy_fake_ip,
        )
    return ReplaySearchProvider(config.replay_corpus)


def _build_online_model(
    config: AppConfig,
    cache: FileCache,
    choice: str,
) -> OpenAICompatibleModelProvider:
    model_id = config.model_id(choice)
    pricing, pricing_configured = config.pricing_for_model(model_id)
    return OpenAICompatibleModelProvider(
        api_key=config.resolved_model_api_key,
        base_url=config.resolved_model_base_url,
        model=model_id,
        cache=cache,
        input_usd_per_m=pricing.input_usd_per_m,
        cached_input_usd_per_m=pricing.cached_input_usd_per_m,
        output_usd_per_m=pricing.output_usd_per_m,
        long_context_threshold_tokens=pricing.long_context_threshold_tokens,
        long_context_input_usd_per_m=pricing.long_context_input_usd_per_m,
        long_context_cached_input_usd_per_m=(
            pricing.long_context_cached_input_usd_per_m
        ),
        pricing_configured=pricing_configured,
        model_choice=choice,
        modalities=config.model_capabilities(choice),
        timeout_seconds=config.model_timeout_seconds,
    )
