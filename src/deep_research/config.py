from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any


SUPPORTED_MODEL_CHOICES = ("qwen", "gpt", "deepseek")
SUPPORTED_MODEL_PROFILES = ("team", *SUPPORTED_MODEL_CHOICES)
SUPPORTED_SEARCH_PROVIDERS = ("openalex", "duckduckgo", "brave", "replay")
MODEL_AGENT_ROLES = (
    "perception",
    "planner",
    "scout",
    "curator",
    "writer",
    "verifier",
)
DEFAULT_TEAM_ROUTES = {
    "perception": "gpt",
    "planner": "gpt",
    "scout": "deepseek",
    "curator": "deepseek",
    "writer": "gpt",
    "verifier": "qwen",
}
SUPPORTED_INPUT_MODALITIES = ("text", "document", "image", "audio")
MODEL_DISPLAY_NAMES = {
    "qwen": "Qwen",
    "gpt": "GPT",
    "deepseek": "DeepSeek",
}


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Declared token pricing for one exact gateway model ID.

    ``None`` means that the gateway may bill that token class but the operator
    has not supplied a rate. It is intentionally distinct from ``0.0``: Qwen
    and DeepSeek are configured as free, while an unknown model remains
    unpriced rather than silently appearing free.
    """

    input_usd_per_m: float | None
    cached_input_usd_per_m: float | None
    output_usd_per_m: float | None
    long_context_threshold_tokens: int | None = None
    long_context_input_usd_per_m: float | None = None
    long_context_cached_input_usd_per_m: float | None = None
    additional_modalities: dict[str, dict[str, float | None]] = field(
        default_factory=dict
    )

    @property
    def has_text_rates(self) -> bool:
        return (
            self.input_usd_per_m is not None
            and self.output_usd_per_m is not None
        )


@dataclass(slots=True)
class BudgetConfig:
    max_iterations: int = 3
    max_search_calls: int = 8
    max_pages: int = 12
    # These are per-run ceilings, persisted into the checkpoint so a later
    # environment change cannot silently buy an unbounded number of retries.
    max_total_iterations: int = 9
    max_total_search_calls: int = 24
    max_total_pages: int = 36
    # Automatic evidence recovery leaves a small, explicit tranche for an
    # operator-approved resume. The persisted total ceilings remain unchanged.
    manual_resume_reserve_iterations: int = 1
    manual_resume_reserve_search_calls: int = 2
    manual_resume_reserve_pages: int = 3


@dataclass(slots=True)
class ClosureConfig:
    threshold: float = 0.75
    min_sources_per_required_slot: int = 2
    min_independent_sources_for_contested_claim: int = 2
    allow_single_authoritative_source: bool = True
    min_slot_relevance: float = 0.45


@dataclass(slots=True)
class AppConfig:
    runs_dir: Path = Path("runs")
    replay_corpus: Path = Path("examples/replay_corpus.json")
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    closure: ClosureConfig = field(default_factory=ClosureConfig)
    cache_dir: Path = Path(".cache/deep-research")
    model_capability_receipt: Path = Path(
        "conformance/model-capability-verification.json"
    )
    model_provider: str = "mock"
    search_provider: str = "replay"
    brave_api_key: str = ""
    allow_rfc2544_proxy_fake_ip: bool = False
    model_choice: str = "deepseek"
    model_profile: str = "team"
    role_models: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_TEAM_ROUTES)
    )
    model_modalities: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "qwen": ("text", "document"),
            "gpt": ("text", "document", "image"),
            "deepseek": ("text", "document"),
        }
    )
    model_api_key: str = ""
    model_base_url: str = ""
    qwen_model: str = ""
    gpt_model: str = ""
    model_input_usd_per_m: float = 0.0
    model_cached_input_usd_per_m: float | None = None
    model_output_usd_per_m: float = 0.0
    model_pricing_file: Path | None = None
    model_pricing: dict[str, ModelPricing] = field(default_factory=dict)
    model_pricing_fallback_configured: bool = False
    model_timeout_seconds: float = 180.0
    # Legacy DeepSeek-specific fields remain available for callers that build
    # AppConfig directly. Shared MODEL_* settings take precedence.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_input_usd_per_m: float = 0.0
    deepseek_output_usd_per_m: float = 0.0

    @classmethod
    def from_env(cls, env_path: Path = Path(".env")) -> "AppConfig":
        values = _read_env_file(env_path)

        def get(name: str, default: str = "") -> str:
            return os.environ.get(name, values.get(name, default))

        def is_set(name: str) -> bool:
            return name in os.environ or name in values

        model_choice = normalize_model_choice(get("DR_DEFAULT_MODEL", "deepseek"))
        model_profile = normalize_model_profile(get("DR_DEFAULT_PROFILE", "team"))
        role_models = {
            role: normalize_model_choice(
                get(
                    f"DR_{role.upper()}_MODEL",
                    DEFAULT_TEAM_ROUTES[role],
                )
            )
            for role in MODEL_AGENT_ROLES
        }
        model_modalities = {
            "qwen": parse_model_modalities(
                get("QWEN_MODALITIES", "text,document"),
                variable="QWEN_MODALITIES",
            ),
            "gpt": parse_model_modalities(
                get("GPT_MODALITIES", "text,document,image"),
                variable="GPT_MODALITIES",
            ),
            "deepseek": parse_model_modalities(
                get("DEEPSEEK_MODALITIES", "text,document"),
                variable="DEEPSEEK_MODALITIES",
            ),
        }
        legacy_api_key = get("DEEPSEEK_API_KEY")
        legacy_base_url = get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        model_api_key = get("MODEL_API_KEY") if is_set("MODEL_API_KEY") else legacy_api_key
        brave_api_key = (
            get("DR_BRAVE_API_KEY")
            if is_set("DR_BRAVE_API_KEY")
            else get("BRAVE_API_KEY")
        )
        model_base_url = (
            get("MODEL_BASE_URL") if is_set("MODEL_BASE_URL") else legacy_base_url
        )
        legacy_input_price = get("DR_DEEPSEEK_INPUT_USD_PER_M", "0")
        legacy_output_price = get("DR_DEEPSEEK_OUTPUT_USD_PER_M", "0")
        model_input_price = float(
            get("DR_MODEL_INPUT_USD_PER_M") or legacy_input_price
        )
        model_output_price = float(
            get("DR_MODEL_OUTPUT_USD_PER_M") or legacy_output_price
        )
        cached_input_value = get("DR_MODEL_CACHED_INPUT_USD_PER_M")
        model_cached_input_price = (
            float(cached_input_value) if cached_input_value.strip() else None
        )
        pricing_file_value = get("DR_MODEL_PRICING_FILE").strip()
        pricing_file = (
            (env_path.parent / pricing_file_value).resolve()
            if pricing_file_value
            else None
        )
        model_pricing = _load_model_pricing(
            pricing_file,
            get("DR_MODEL_PRICING_JSON").strip(),
        )
        fallback_configured = (
            get("DR_MODEL_PRICING_FALLBACK_CONFIGURED", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        # Preserve the legacy non-zero global rates as an explicit fallback,
        # while refusing to treat their historical zero defaults as proof that
        # an arbitrary, unlisted gateway model is free.
        fallback_configured = fallback_configured or any(
            value != 0.0
            for value in (model_input_price, model_output_price)
        )

        return cls(
            runs_dir=Path(get("DR_RUNS_DIR", "runs")),
            replay_corpus=Path(get("DR_REPLAY_CORPUS", "examples/replay_corpus.json")),
            cache_dir=Path(get("DR_CACHE_DIR", ".cache/deep-research")),
            model_capability_receipt=Path(
                get(
                    "DR_MODEL_CAPABILITY_RECEIPT",
                    "conformance/model-capability-verification.json",
                )
            ),
            model_provider=get("DR_MODEL_PROVIDER", "mock"),
            search_provider=get("DR_SEARCH_PROVIDER", "replay"),
            brave_api_key=brave_api_key,
            allow_rfc2544_proxy_fake_ip=get(
                "DR_ALLOW_RFC2544_PROXY_FAKE_IP", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            model_choice=model_choice,
            model_profile=model_profile,
            role_models=role_models,
            model_modalities=model_modalities,
            model_api_key=model_api_key,
            model_base_url=model_base_url,
            qwen_model=get("QWEN_MODEL"),
            gpt_model=get("GPT_MODEL"),
            model_input_usd_per_m=model_input_price,
            model_cached_input_usd_per_m=model_cached_input_price,
            model_output_usd_per_m=model_output_price,
            model_pricing_file=pricing_file,
            model_pricing=model_pricing,
            model_pricing_fallback_configured=fallback_configured,
            model_timeout_seconds=float(
                get("DR_MODEL_TIMEOUT_SECONDS", "180")
            ),
            deepseek_api_key=legacy_api_key,
            deepseek_base_url=legacy_base_url,
            deepseek_model=get("DEEPSEEK_MODEL", "deepseek-chat"),
            deepseek_input_usd_per_m=float(legacy_input_price),
            deepseek_output_usd_per_m=float(legacy_output_price),
            budget=BudgetConfig(
                max_iterations=int(get("DR_MAX_ITERATIONS", "3")),
                max_search_calls=int(get("DR_MAX_SEARCH_CALLS", "8")),
                max_pages=int(get("DR_MAX_PAGES", "12")),
                max_total_iterations=int(get("DR_MAX_TOTAL_ITERATIONS", "9")),
                max_total_search_calls=int(get("DR_MAX_TOTAL_SEARCH_CALLS", "24")),
                max_total_pages=int(get("DR_MAX_TOTAL_PAGES", "36")),
                manual_resume_reserve_iterations=max(
                    0, int(get("DR_MANUAL_RESUME_RESERVE_ITERATIONS", "1"))
                ),
                manual_resume_reserve_search_calls=max(
                    0, int(get("DR_MANUAL_RESUME_RESERVE_SEARCH_CALLS", "2"))
                ),
                manual_resume_reserve_pages=max(
                    0, int(get("DR_MANUAL_RESUME_RESERVE_PAGES", "3"))
                ),
            ),
            closure=ClosureConfig(
                threshold=float(get("DR_CLOSURE_THRESHOLD", "0.75")),
                min_sources_per_required_slot=int(get("DR_MIN_SOURCES_PER_REQUIRED_SLOT", "2")),
                min_independent_sources_for_contested_claim=int(
                    get("DR_MIN_INDEPENDENT_SOURCES_FOR_CONTESTED_CLAIM", "2")
                ),
                allow_single_authoritative_source=get(
                    "DR_ALLOW_SINGLE_AUTHORITATIVE_SOURCE", "true"
                ).strip().lower() in {"1", "true", "yes", "on"},
                min_slot_relevance=float(get("DR_MIN_SLOT_RELEVANCE", "0.45")),
            ),
        )

    @property
    def resolved_model_api_key(self) -> str:
        if self.model_api_key:
            return self.model_api_key
        return self.deepseek_api_key if self.model_provider == "deepseek" else ""

    @property
    def resolved_model_base_url(self) -> str:
        if self.model_base_url:
            return self.model_base_url
        return self.deepseek_base_url if self.model_provider == "deepseek" else ""

    @property
    def resolved_brave_api_key(self) -> str:
        return self.brave_api_key.strip()

    def normalized_search_provider(self) -> str:
        if not isinstance(self.search_provider, str):
            raise ValueError(
                "DR_SEARCH_PROVIDER must be one of: "
                + ", ".join(SUPPORTED_SEARCH_PROVIDERS)
            )
        provider = self.search_provider.strip().casefold()
        if provider not in SUPPORTED_SEARCH_PROVIDERS:
            raise ValueError(
                "DR_SEARCH_PROVIDER must be one of: "
                + ", ".join(SUPPORTED_SEARCH_PROVIDERS)
            )
        return provider

    @property
    def search_provider_configured(self) -> bool:
        try:
            provider = self.normalized_search_provider()
        except ValueError:
            return False
        return provider != "brave" or bool(self.resolved_brave_api_key)

    def require_search_provider(self) -> str:
        provider = self.normalized_search_provider()
        if provider == "brave" and not self.resolved_brave_api_key:
            raise ValueError(
                "DR_BRAVE_API_KEY is empty. Fill it in the project .env file."
            )
        return provider

    def model_id(self, choice: object | None = None, *, required: bool = True) -> str:
        selected = normalize_model_choice(
            self.model_choice if choice is None else choice
        )
        model = {
            "qwen": self.qwen_model,
            "gpt": self.gpt_model,
            "deepseek": self.deepseek_model,
        }[selected].strip()
        if required and not model:
            variable = f"{selected.upper()}_MODEL"
            raise ValueError(f"{variable} is empty. Fill it in the project .env file.")
        return model

    def pricing_for_model(self, model_id: object) -> tuple[ModelPricing, bool]:
        """Return exact-ID pricing and whether it is operator-configured."""

        normalized = _normalize_model_id(model_id)
        pricing = self.model_pricing.get(normalized)
        if pricing is None and "/" in normalized:
            pricing = self.model_pricing.get(normalized.rsplit("/", 1)[-1])
        if pricing is not None:
            return pricing, True
        fallback_configured = self.model_pricing_fallback_configured or any(
            value is not None and value != 0.0
            for value in (
                self.model_input_usd_per_m,
                self.model_cached_input_usd_per_m,
                self.model_output_usd_per_m,
            )
        )
        if fallback_configured:
            return (
                ModelPricing(
                    input_usd_per_m=self.model_input_usd_per_m,
                    cached_input_usd_per_m=self.model_cached_input_usd_per_m,
                    output_usd_per_m=self.model_output_usd_per_m,
                ),
                True,
            )
        return ModelPricing(None, None, None), False

    def select_model(self, choice: object | None) -> str:
        selected = normalize_model_choice(
            self.model_choice if choice is None else choice
        )
        self.model_choice = selected
        return selected

    def model_options(self) -> list[dict[str, object]]:
        shared_ready = bool(
            self.resolved_model_api_key and self.resolved_model_base_url
        )
        options: list[dict[str, object]] = []
        for choice in SUPPORTED_MODEL_CHOICES:
            verified = self.verified_model_capabilities(choice)
            options.append({
                "id": choice,
                "label": MODEL_DISPLAY_NAMES[choice],
                "model": self.model_id(choice, required=False),
                "configured": shared_ready
                and bool(self.model_id(choice, required=False)),
                "modalities": list(self.model_capabilities(choice)),
                "verified_modalities": list(verified),
                "capability_status": (
                    "runtime_probe_partially_verified"
                    if verified
                    else "declared_unverified"
                ),
            })
        return options

    def verified_model_capabilities(self, choice: object) -> tuple[str, ...]:
        selected = normalize_model_choice(choice)
        model_id = self.model_id(selected, required=False)
        if not model_id or not self.resolved_model_base_url:
            return ()
        path = self.model_capability_receipt
        try:
            if not path.is_file() or path.stat().st_size > 65_536:
                return ()
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ()
        if not isinstance(payload, dict) or payload.get("schema_version") != "model-capability-verification/1.0":
            return ()
        expected_gateway_hash = hashlib.sha256(
            self.resolved_model_base_url.rstrip("/").encode()
        ).hexdigest()
        if payload.get("gateway_base_url_sha256") != expected_gateway_hash:
            return ()
        verified: set[str] = set()
        for record in payload.get("verifications", []):
            if not isinstance(record, dict):
                continue
            if (
                record.get("result") != "passed"
                or record.get("model_choice") != selected
                or record.get("model_id") != model_id
            ):
                continue
            modality = str(record.get("modality") or "")
            if modality in SUPPORTED_INPUT_MODALITIES:
                verified.add(modality)
        declared = set(self.model_capabilities(selected))
        return tuple(
            modality
            for modality in SUPPORTED_INPUT_MODALITIES
            if modality in verified and modality in declared
        )

    def model_capabilities(self, choice: object) -> tuple[str, ...]:
        selected = normalize_model_choice(choice)
        configured = self.model_modalities.get(selected, ("text", "document"))
        return tuple(
            modality
            for modality in SUPPORTED_INPUT_MODALITIES
            if modality in configured
        )

    def profile_routes(self, profile: object | None = None) -> dict[str, str]:
        selected = normalize_model_profile(
            self.model_profile if profile is None else profile
        )
        if selected == "team":
            return {
                role: normalize_model_choice(self.role_models[role])
                for role in MODEL_AGENT_ROLES
            }
        return {role: selected for role in MODEL_AGENT_ROLES}

    def select_profile(self, profile: object | None) -> str:
        selected = normalize_model_profile(
            self.model_profile if profile is None else profile
        )
        self.model_profile = selected
        if selected != "team":
            self.model_choice = selected
        return selected

    def profile_options(self) -> list[dict[str, object]]:
        configured_models = {
            str(item["id"]): bool(item["configured"])
            for item in self.model_options()
        }
        options: list[dict[str, object]] = []
        for profile in SUPPORTED_MODEL_PROFILES:
            routes = self.profile_routes(profile)
            used = sorted(set(routes.values()))
            perception_choice = routes["perception"]
            verified_modalities = self.verified_model_capabilities(
                perception_choice
            )
            options.append(
                {
                    "id": profile,
                    "label": (
                        "Qwen + GPT + DeepSeek 协作"
                        if profile == "team"
                        else MODEL_DISPLAY_NAMES[profile]
                    ),
                    "configured": all(configured_models.get(item, False) for item in used),
                    "routes": routes,
                    "models": used,
                    "perception_model": routes["perception"],
                    "input_modalities": list(
                        self.model_capabilities(routes["perception"])
                    ),
                    "verified_input_modalities": list(verified_modalities),
                    "capability_status": (
                        "runtime_probe_partially_verified"
                        if verified_modalities
                        else "declared_unverified"
                    ),
                }
            )
        return options

    def require_online_profile(
        self,
        profile: object | None = None,
        *,
        required_modalities: set[str] | None = None,
    ) -> str:
        selected = self.select_profile(profile)
        if not self.resolved_model_api_key:
            raise ValueError(
                "MODEL_API_KEY is empty. Fill it in the project .env file."
            )
        if not self.resolved_model_base_url:
            raise ValueError(
                "MODEL_BASE_URL is empty. Fill it in the project .env file."
            )
        routes = self.profile_routes(selected)
        for choice in sorted(set(routes.values())):
            self.model_id(choice)
        required = set(required_modalities or ())
        unsupported = required - set(
            self.model_capabilities(routes["perception"])
        )
        if unsupported:
            model = routes["perception"]
            values = ", ".join(sorted(unsupported))
            raise ValueError(
                f"The perception route ({model}) does not declare support for: {values}. "
                f"Update {model.upper()}_MODALITIES only after the shared gateway capability is verified."
            )
        return selected

    def require_online_model(self, choice: object | None = None) -> str:
        selected = self.select_model(choice)
        if not self.resolved_model_api_key:
            raise ValueError(
                "MODEL_API_KEY is empty. Fill it in the project .env file."
            )
        if not self.resolved_model_base_url:
            raise ValueError(
                "MODEL_BASE_URL is empty. Fill it in the project .env file."
            )
        self.model_id(selected)
        return selected


def normalize_model_choice(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("model must be one of: qwen, gpt, deepseek")
    choice = value.strip().casefold()
    if choice not in SUPPORTED_MODEL_CHOICES:
        raise ValueError("model must be one of: qwen, gpt, deepseek")
    return choice


def normalize_model_profile(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("model profile must be one of: team, qwen, gpt, deepseek")
    profile = value.strip().casefold()
    if profile not in SUPPORTED_MODEL_PROFILES:
        raise ValueError("model profile must be one of: team, qwen, gpt, deepseek")
    return profile


def parse_model_modalities(value: object, *, variable: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{variable} must be a comma-separated modality list")
    requested = {
        item.strip().casefold()
        for item in value.split(",")
        if item.strip()
    }
    unknown = requested - set(SUPPORTED_INPUT_MODALITIES)
    if unknown:
        raise ValueError(
            f"{variable} contains unsupported modalities: {', '.join(sorted(unknown))}"
        )
    if "text" not in requested:
        raise ValueError(f"{variable} must include text")
    if "document" not in requested:
        requested.add("document")
    return tuple(
        modality
        for modality in SUPPORTED_INPUT_MODALITIES
        if modality in requested
    )


def _normalize_model_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("model pricing requires a non-empty model ID")
    return value.strip().casefold()


def _load_model_pricing(
    pricing_file: Path | None,
    inline_payload: str,
) -> dict[str, ModelPricing]:
    """Load an auditable model-ID price catalog from file and/or environment."""

    records: dict[str, object] = {}
    if pricing_file is not None:
        try:
            raw_file = json.loads(pricing_file.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError(
                f"DR_MODEL_PRICING_FILE does not exist: {pricing_file}"
            ) from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"DR_MODEL_PRICING_FILE is not valid JSON: {pricing_file}"
            ) from error
        records.update(_pricing_records(raw_file, str(pricing_file)))
    if inline_payload:
        try:
            raw_inline = json.loads(inline_payload)
        except json.JSONDecodeError as error:
            raise ValueError("DR_MODEL_PRICING_JSON is not valid JSON") from error
        records.update(_pricing_records(raw_inline, "DR_MODEL_PRICING_JSON"))
    return {
        _normalize_model_id(model_id): _parse_model_pricing(model_id, record)
        for model_id, record in records.items()
    }


def _pricing_records(payload: object, source: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError(f"{source} must contain a JSON object")
    models = payload.get("models", payload)
    if not isinstance(models, dict) or not models:
        raise ValueError(f"{source} must contain a non-empty models object")
    records: dict[str, object] = {}
    for model_id, record in models.items():
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError(f"{source} contains an invalid model ID")
        if not isinstance(record, dict):
            raise ValueError(f"{source} pricing for {model_id} must be an object")
        records[model_id] = record
    return records


def _parse_model_pricing(model_id: str, record: object) -> ModelPricing:
    if not isinstance(record, dict):
        raise ValueError(f"pricing for {model_id} must be an object")
    text = record.get("text", record)
    if not isinstance(text, dict):
        raise ValueError(f"pricing.text for {model_id} must be an object")
    long_context = record.get("long_context", {})
    if long_context is None:
        long_context = {}
    if not isinstance(long_context, dict):
        raise ValueError(f"pricing.long_context for {model_id} must be an object")
    threshold = _optional_positive_integer(
        long_context.get("input_threshold_tokens"),
        f"pricing.long_context.input_threshold_tokens for {model_id}",
    )
    modalities: dict[str, dict[str, float | None]] = {}
    for modality in ("audio", "image"):
        raw_modality = record.get(modality)
        if raw_modality is None:
            continue
        if not isinstance(raw_modality, dict):
            raise ValueError(f"pricing.{modality} for {model_id} must be an object")
        modalities[modality] = {
            "input_usd_per_m": _optional_rate(
                raw_modality.get("input_usd_per_m"),
                f"pricing.{modality}.input_usd_per_m for {model_id}",
            ),
            "cached_input_usd_per_m": _optional_rate(
                raw_modality.get("cached_input_usd_per_m"),
                f"pricing.{modality}.cached_input_usd_per_m for {model_id}",
            ),
            "output_usd_per_m": _optional_rate(
                raw_modality.get("output_usd_per_m"),
                f"pricing.{modality}.output_usd_per_m for {model_id}",
            ),
        }
    return ModelPricing(
        input_usd_per_m=_required_rate(
            text.get("input_usd_per_m"),
            f"pricing.text.input_usd_per_m for {model_id}",
        ),
        cached_input_usd_per_m=_optional_rate(
            text.get("cached_input_usd_per_m"),
            f"pricing.text.cached_input_usd_per_m for {model_id}",
        ),
        output_usd_per_m=_required_rate(
            text.get("output_usd_per_m"),
            f"pricing.text.output_usd_per_m for {model_id}",
        ),
        long_context_threshold_tokens=threshold,
        long_context_input_usd_per_m=_optional_rate(
            long_context.get("input_usd_per_m"),
            f"pricing.long_context.input_usd_per_m for {model_id}",
        ),
        long_context_cached_input_usd_per_m=_optional_rate(
            long_context.get("cached_input_usd_per_m"),
            f"pricing.long_context.cached_input_usd_per_m for {model_id}",
        ),
        additional_modalities=modalities,
    )


def _required_rate(value: object, label: str) -> float:
    if value is None:
        raise ValueError(f"{label} is required")
    parsed = _optional_rate(value, label)
    if parsed is None:
        raise ValueError(f"{label} is required")
    return parsed


def _optional_rate(value: object, label: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative number or null")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a non-negative number or null") from error
    if parsed < 0 or parsed == float("inf") or parsed != parsed:
        raise ValueError(f"{label} must be a non-negative finite number or null")
    return parsed


def _optional_positive_integer(value: object, label: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer or null")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a positive integer or null") from error
    if parsed <= 0 or str(parsed) != str(value).strip():
        raise ValueError(f"{label} must be a positive integer or null")
    return parsed


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
