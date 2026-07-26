from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from .config import AppConfig, SUPPORTED_MODEL_PROFILES
from .engine import ResearchEngine
from .evaluation import evaluate_jsonl
from .providers import MockModelProvider, ReplaySearchProvider, build_model_team
from .report import generate_html_report
from .storage import RunStore
from .webapp import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verifiable deep research harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run or resume a research task")
    run.add_argument("--question", required=True)
    run.add_argument("--run-id")
    run.add_argument(
        "--offline",
        action="store_true",
        help="Use deterministic mock/replay providers regardless of .env",
    )
    run.add_argument(
        "--profile",
        choices=SUPPORTED_MODEL_PROFILES,
        help="Role-routing profile; defaults to DR_DEFAULT_PROFILE",
    )

    inspect = subparsers.add_parser("inspect", help="Inspect the latest checkpoint")
    inspect.add_argument("--run-id", required=True)

    evaluate = subparsers.add_parser("eval", help="Evaluate a JSONL task set")
    evaluate.add_argument("--dataset", default="examples/tasks.jsonl")
    evaluate.add_argument("--output", default="runs/evaluation.json")
    evaluate.add_argument("--offline", action="store_true")

    subparsers.add_parser("config", help="Show provider configuration without secrets")

    report = subparsers.add_parser("report", help="Generate an HTML report for a run")
    report.add_argument("--run-id", required=True)
    report.add_argument("--output")

    web = subparsers.add_parser("serve", help="Start the interactive research UI")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    return parser


async def _run(
    question: str,
    run_id: str | None,
    offline: bool,
    profile: str | None = None,
) -> None:
    config = AppConfig.from_env()
    engine = _engine(config, offline, profile)
    state = await engine.run(question, run_id)
    print(json.dumps(state.as_dict(), ensure_ascii=False, indent=2))


def _engine(
    config: AppConfig,
    offline: bool = False,
    profile: str | None = None,
) -> ResearchEngine:
    if offline:
        model = MockModelProvider()
        search = ReplaySearchProvider(config.replay_corpus)
    else:
        selected_profile = config.require_online_profile(
            config.model_profile if profile is None else profile
        )
        model, search = build_model_team(config, selected_profile)
    return ResearchEngine(config, model, search)


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "serve":
        serve(args.host, args.port)
        return
    if args.command == "run":
        asyncio.run(_run(args.question, args.run_id, args.offline, args.profile))
        return
    config = AppConfig.from_env()
    if args.command == "config":
        print(
            json.dumps(
                {
                    "model_provider": config.model_provider,
                    "search_provider": config.search_provider,
                    "default_model": config.model_choice,
                    "default_profile": config.model_profile,
                    "models": config.model_options(),
                    "profiles": config.profile_options(),
                    "role_routes": config.profile_routes("team"),
                    "model_base_url_set": bool(config.resolved_model_base_url),
                    "model_api_key_set": bool(config.resolved_model_api_key),
                    "brave_api_key_set": bool(config.resolved_brave_api_key),
                    "search_configured": config.search_provider_configured,
                    "cache_dir": str(config.cache_dir),
                    "runs_dir": str(config.runs_dir),
                },
                indent=2,
            )
        )
        return
    if args.command == "eval":
        records = asyncio.run(
            evaluate_jsonl(
                _engine(config, args.offline), Path(args.dataset), Path(args.output)
            )
        )
        print(json.dumps([asdict(record) for record in records], indent=2))
        return
    if args.command == "report":
        final_json = config.runs_dir / args.run_id / "final.json"
        output = Path(args.output) if args.output else final_json.with_name("report.html")
        print(generate_html_report(final_json, output))
        return
    state = RunStore(config.runs_dir, args.run_id).latest()
    if state is None:
        raise SystemExit(f"No checkpoint found for run {args.run_id}")
    print(json.dumps(state.as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
