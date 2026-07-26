import tempfile
import unittest
from pathlib import Path

from deep_research.config import AppConfig, DEFAULT_TEAM_ROUTES
from deep_research.cli import _engine
from deep_research.engine import ResearchEngine
from deep_research.providers.factory import build_model_team, build_providers
from deep_research.providers.mock import MockModelProvider, ReplaySearchProvider
from deep_research.providers.team import ModelProviderTeam
from deep_research.storage import RunStore


class TaggedMockModel(MockModelProvider):
    def __init__(
        self,
        choice: str,
        model: str,
        modalities: tuple[str, ...] = ("text", "document"),
    ) -> None:
        self.model_choice = choice
        self.model = model
        self.modalities = modalities


class ModelTeamConfigurationTest(unittest.TestCase):
    def configured(self, root: Path) -> AppConfig:
        return AppConfig(
            runs_dir=root / "runs",
            cache_dir=root / "cache",
            replay_corpus=Path("examples/replay_corpus.json"),
            model_provider="openai_compatible",
            search_provider="replay",
            model_profile="team",
            role_models=dict(DEFAULT_TEAM_ROUTES),
            model_api_key="shared-key",
            model_base_url="https://gateway.example/v1",
            qwen_model="qwen3.6-35b-a3b",
            gpt_model="gpt-5.4-nano",
            deepseek_model="deepseek-v4-flash",
            model_modalities={
                "qwen": ("text", "document"),
                "gpt": ("text", "document", "image"),
                "deepseek": ("text", "document"),
            },
        )

    def test_team_builds_three_exact_models_and_role_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.configured(Path(directory))
            team, _ = build_model_team(config, "team")

        self.assertEqual(team.routes, DEFAULT_TEAM_ROUTES)
        snapshot = team.route_snapshot()
        self.assertEqual(snapshot["planner"]["model"], "gpt-5.4-nano")
        self.assertEqual(snapshot["scout"]["model"], "deepseek-v4-flash")
        self.assertEqual(snapshot["verifier"]["model"], "qwen3.6-35b-a3b")
        self.assertIn("image", snapshot["perception"]["modalities"])

    def test_single_model_profile_preserves_its_declared_modalities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.configured(Path(directory))
            model, _ = build_providers(config, "gpt")

        self.assertEqual(model.model, "gpt-5.4-nano")
        self.assertEqual(model.modalities, ("text", "document", "image"))

    def test_profile_contract_distinguishes_declared_capability_from_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.configured(Path(directory))
            team = next(item for item in config.profile_options() if item["id"] == "team")

        self.assertEqual(team["perception_model"], "gpt")
        self.assertEqual(team["input_modalities"], ["text", "document", "image"])
        self.assertEqual(team["capability_status"], "declared_unverified")

    def test_cli_engine_uses_team_profile_by_default_and_honors_single_model_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.configured(Path(directory))
            team_engine = _engine(config)
            qwen_engine = _engine(config, profile="qwen")

        self.assertIsInstance(team_engine.model, ModelProviderTeam)
        self.assertEqual(team_engine.model.profile, "team")
        self.assertEqual(team_engine.model.routes, DEFAULT_TEAM_ROUTES)
        self.assertEqual(set(qwen_engine.model.routes.values()), {"qwen"})


class ModelTeamExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_run_records_all_three_models_by_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            providers = {
                "qwen": TaggedMockModel("qwen", "qwen3.6-35b-a3b"),
                "gpt": TaggedMockModel(
                    "gpt", "gpt-5.4-nano", ("text", "document", "image")
                ),
                "deepseek": TaggedMockModel("deepseek", "deepseek-v4-flash"),
            }
            team = ModelProviderTeam(
                providers,
                dict(DEFAULT_TEAM_ROUTES),
                profile="team",
            )
            config = AppConfig(
                runs_dir=root / "runs",
                replay_corpus=Path("examples/replay_corpus.json"),
            )
            store = RunStore(config.runs_dir, "team-routing")
            store.store_input_attachment(
                name="context.txt",
                media_type="text/plain",
                modality="text",
                data=b"A user supplied context note.",
            )

            state = await ResearchEngine(
                config,
                team,
                ReplaySearchProvider(config.replay_corpus),
            ).run(
                "Who created Python and when was it first released?",
                run_id="team-routing",
            )

        routed = {
            item.agent_id: (item.model_choice, item.model_id)
            for item in state.agent_invocations
            if item.operation
            in {
                "perceive_inputs",
                "plan",
                "generate_queries",
                "extract_evidence",
                "draft",
                "verify",
            }
            and item.execution_mode == "executed"
        }
        self.assertEqual(routed["perception"], ("gpt", "gpt-5.4-nano"))
        self.assertEqual(routed["planner"], ("gpt", "gpt-5.4-nano"))
        self.assertEqual(routed["scout"], ("deepseek", "deepseek-v4-flash"))
        self.assertEqual(routed["curator"], ("deepseek", "deepseek-v4-flash"))
        self.assertEqual(routed["writer"], ("gpt", "gpt-5.4-nano"))
        self.assertEqual(routed["verifier"], ("qwen", "qwen3.6-35b-a3b"))
        self.assertEqual(
            set(state.methodology["model_routes"]),
            {"perception", "planner", "scout", "curator", "writer", "verifier"},
        )
        self.assertEqual(
            next(
                item for item in state.agent_invocations if item.agent_id == "perception"
            ).input_modalities,
            ["text"],
        )


if __name__ == "__main__":
    unittest.main()
