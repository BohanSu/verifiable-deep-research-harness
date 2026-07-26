import json
import hashlib
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from deep_research.config import AppConfig, normalize_model_choice
from deep_research.providers.deepseek import OpenAICompatibleModelProvider
from deep_research.providers.factory import build_providers
from deep_research.providers.web import BraveSearchProvider, OpenAlexSearchProvider
from deep_research.system_contract import system_contract


class ConfigTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_loads_deepseek_settings_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "DR_MODEL_PROVIDER=deepseek\nDEEPSEEK_API_KEY=test-secret\n",
                encoding="utf-8",
            )
            config = AppConfig.from_env(path)
            self.assertEqual(config.model_provider, "deepseek")
            self.assertEqual(config.deepseek_api_key, "test-secret")
            self.assertEqual(config.resolved_model_api_key, "test-secret")

    def test_rfc2544_fake_ip_compatibility_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DR_SEARCH_PROVIDER=duckduckgo\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                config = AppConfig.from_env(path)
            self.assertFalse(config.allow_rfc2544_proxy_fake_ip)

            path.write_text(
                "DR_SEARCH_PROVIDER=duckduckgo\n"
                "DR_ALLOW_RFC2544_PROXY_FAKE_IP=true\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                config = AppConfig.from_env(path)
            self.assertTrue(config.allow_rfc2544_proxy_fake_ip)
            _model, search = build_providers(config)
            self.assertTrue(search.allow_rfc2544_proxy_fake_ip)

    def test_brave_search_uses_explicit_key_and_configured_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".env"
            path.write_text(
                "DR_SEARCH_PROVIDER=brave\n"
                "DR_BRAVE_API_KEY=brave-test-secret\n"
                f"DR_CACHE_DIR={root / 'cache'}\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                config = AppConfig.from_env(path)
            self.assertEqual(config.require_search_provider(), "brave")
            self.assertTrue(config.search_provider_configured)
            _model, search = build_providers(config)

        self.assertIsInstance(search, BraveSearchProvider)
        self.assertTrue(search.allow_rfc2544_proxy_fake_ip is False)

    def test_brave_search_refuses_missing_key(self) -> None:
        config = AppConfig(search_provider="brave")

        self.assertFalse(config.search_provider_configured)
        with self.assertRaisesRegex(ValueError, "DR_BRAVE_API_KEY is empty"):
            config.require_search_provider()

    def test_openalex_search_requires_no_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                search_provider="openalex",
                cache_dir=Path(directory) / "cache",
            )
            self.assertEqual(config.require_search_provider(), "openalex")
            self.assertTrue(config.search_provider_configured)
            _model, search = build_providers(config)

        self.assertIsInstance(search, OpenAlexSearchProvider)

    def test_shared_gateway_exposes_three_fixed_model_choices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "DR_MODEL_PROVIDER=openai_compatible",
                        "DR_DEFAULT_MODEL=qwen",
                        "MODEL_API_KEY=shared-secret",
                        "MODEL_BASE_URL=https://gateway.example/v1",
                        "DR_MODEL_TIMEOUT_SECONDS=240",
                        "QWEN_MODEL=qwen-model",
                        "GPT_MODEL=gpt-model",
                        "DEEPSEEK_MODEL=deepseek-model",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "DR_MODEL_PROVIDER": "openai_compatible",
                    "DR_DEFAULT_MODEL": "qwen",
                    "MODEL_API_KEY": "shared-secret",
                    "MODEL_BASE_URL": "https://gateway.example/v1",
                    "DR_MODEL_TIMEOUT_SECONDS": "240",
                    "QWEN_MODEL": "qwen-model",
                    "GPT_MODEL": "gpt-model",
                    "DEEPSEEK_MODEL": "deepseek-model",
                },
                clear=False,
            ):
                config = AppConfig.from_env(path)

            self.assertEqual(config.model_choice, "qwen")
            self.assertEqual(config.model_timeout_seconds, 240)
            self.assertEqual(config.model_id("gpt"), "gpt-model")
            self.assertTrue(all(item["configured"] for item in config.model_options()))
            model, _search = build_providers(config, "deepseek")
            self.assertIsInstance(model, OpenAICompatibleModelProvider)
            self.assertEqual(model.model_choice, "deepseek")
            self.assertEqual(model.model, "deepseek-model")
            self.assertEqual(model.base_url, "https://gateway.example/v1")
            self.assertEqual(model.timeout_seconds, 240)

    def test_model_pricing_catalog_uses_exact_model_ids_and_preserves_free_rates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "pricing.json"
            catalog.write_text(
                json.dumps(
                    {
                        "models": {
                            "gpt-5.5": {
                                "text": {
                                    "input_usd_per_m": 5.0,
                                    "cached_input_usd_per_m": 0.5,
                                    "output_usd_per_m": 30.0,
                                },
                                "long_context": {
                                    "input_threshold_tokens": 272000,
                                    "input_usd_per_m": None,
                                },
                            },
                            "qwen3.6-35b-a3b": {
                                "text": {
                                    "input_usd_per_m": 0.0,
                                    "cached_input_usd_per_m": 0.0,
                                    "output_usd_per_m": 0.0,
                                }
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = root / ".env"
            env.write_text("DR_MODEL_PRICING_FILE=pricing.json\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                config = AppConfig.from_env(env)

            gpt, gpt_configured = config.pricing_for_model("gateway/gpt-5.5")
            qwen, qwen_configured = config.pricing_for_model("qwen3.6-35b-a3b")
            unknown, unknown_configured = config.pricing_for_model("other-model")
            self.assertTrue(gpt_configured)
            self.assertEqual(gpt.input_usd_per_m, 5.0)
            self.assertEqual(gpt.cached_input_usd_per_m, 0.5)
            self.assertEqual(gpt.output_usd_per_m, 30.0)
            self.assertEqual(gpt.long_context_threshold_tokens, 272000)
            self.assertIsNone(gpt.long_context_input_usd_per_m)
            self.assertTrue(qwen_configured)
            self.assertEqual(qwen.input_usd_per_m, 0.0)
            self.assertEqual(qwen.output_usd_per_m, 0.0)
            self.assertFalse(unknown_configured)
            self.assertIsNone(unknown.input_usd_per_m)

    def test_model_choice_rejects_arbitrary_gateway_model_ids(self) -> None:
        for value in ("gpt-4.1", "qwen-plus", "", None, ["deepseek"]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "qwen, gpt, deepseek"):
                    normalize_model_choice(value)

    def test_capability_receipt_is_bound_to_exact_gateway_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "capabilities.json"
            gateway = "https://gateway.example/v1"
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": "model-capability-verification/1.0",
                        "gateway_base_url_sha256": hashlib.sha256(
                            gateway.encode()
                        ).hexdigest(),
                        "verifications": [
                            {
                                "model_choice": "gpt",
                                "model_id": "gpt-verified",
                                "modality": "image",
                                "result": "passed",
                            },
                            {
                                "model_choice": "gpt",
                                "model_id": "gpt-verified",
                                "modality": "audio",
                                "result": "failed",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = AppConfig(
                model_api_key="shared-key",
                model_base_url=gateway,
                gpt_model="gpt-verified",
                qwen_model="qwen-model",
                deepseek_model="deepseek-model",
                model_capability_receipt=receipt,
                model_modalities={
                    "qwen": ("text", "document"),
                    "gpt": ("text", "document", "image", "audio"),
                    "deepseek": ("text", "document"),
                },
            )

            self.assertEqual(config.verified_model_capabilities("gpt"), ("image",))
            team = next(item for item in config.profile_options() if item["id"] == "team")
            self.assertEqual(team["verified_input_modalities"], ["image"])
            self.assertEqual(
                team["capability_status"], "runtime_probe_partially_verified"
            )

            config.gpt_model = "gpt-changed"
            self.assertEqual(config.verified_model_capabilities("gpt"), ())
            config.gpt_model = "gpt-verified"
            config.model_base_url = "https://other-gateway.example/v1"
            self.assertEqual(config.verified_model_capabilities("gpt"), ())

    def test_protocol_adapter_dependencies_are_exactly_pinned(self) -> None:
        manifest = tomllib.loads(
            (self.ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]

        self.assertEqual(manifest["dependencies"], ["ag-ui-protocol==0.1.19"])
        self.assertEqual(
            manifest["optional-dependencies"]["mcp"],
            [
                "mcp==1.28.1",
                "sse-starlette==2.4.1",
                "starlette==0.38.6; python_version < '3.14'",
                "starlette==0.48.0; python_version >= '3.14'",
            ],
        )
        self.assertEqual(
            manifest["optional-dependencies"]["a2a"],
            ["a2a-sdk[http-server]==1.1.1"],
        )

    def test_capability_registry_matches_protocol_manifest_and_ts_lock(self) -> None:
        contract = system_contract()
        records = {
            item["name"]: item
            for item in contract["official_verification"]["packages"]
        }
        self.assertEqual(records["A2A"]["expected_sdk"], "1.1.1")
        self.assertEqual(records["MCP"]["expected_sdk"], "1.28.1")
        self.assertEqual(
            records["AG-UI"]["expected_sdk"],
            "Python 0.1.19 / TypeScript 0.0.57",
        )

        package_json = json.loads(
            (self.ROOT / "conformance/agui/package.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            package_json["dependencies"],
            {"@ag-ui/client": "0.0.57", "@ag-ui/core": "0.0.57", "rxjs": "7.8.1"},
        )
        lock = json.loads(
            (self.ROOT / "conformance/agui/package-lock.json").read_text(
                encoding="utf-8"
            )
        )
        for package in ("@ag-ui/client", "@ag-ui/core"):
            self.assertEqual(
                lock["packages"][f"node_modules/{package}"]["version"],
                "0.0.57",
            )


if __name__ == "__main__":
    unittest.main()
