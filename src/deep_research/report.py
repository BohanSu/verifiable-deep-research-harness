from __future__ import annotations

import html
import json
from pathlib import Path


def generate_html_report(final_json: Path, output_path: Path) -> Path:
    state = json.loads(final_json.read_text(encoding="utf-8"))
    plan = state.get("plan") or {}
    closure = state.get("closure") if isinstance(state.get("closure"), dict) else None
    verification = (
        state.get("verification")
        if isinstance(state.get("verification"), dict)
        else None
    )
    delivery = (
        state.get("answer_delivery")
        if isinstance(state.get("answer_delivery"), dict)
        else {}
    )
    counters = state.get("counters") if isinstance(state.get("counters"), dict) else {}
    evidence_rows = "".join(
        _evidence_row(item) for item in state.get("evidence", [])
    ) or '<tr><td colspan="6">No evidence</td></tr>'
    query_items = "".join(
        f'<li><code>{_h(item["strategy"])}</code> {_h(item["text"])}</li>'
        for item in state.get("queries", [])
    ) or "<li>No queries</li>"
    slot_items = "".join(
        f'<li><strong>{_h(item["description"])}</strong>: '
        f'{_h(item.get("value") or "unresolved")} '
        f'<span class="score">{_format_number(item.get("confidence"), 2)}</span></li>'
        for item in plan.get("slots", [])
    ) or "<li>No plan</li>"
    failure_items = "".join(
        f'<li><code>{_h(item.get("type", "error"))}</code> '
        f'{_h(item.get("reason", ""))}</li>'
        for item in state.get("failures", [])
    ) or "<li>None</li>"
    verification_items = "".join(
        f'<li class="{_h(item["status"])}"><strong>{_h(item["status"])}</strong> '
        f'{_h(item["claim"])}: {_h(item["reason"])}</li>'
        for item in (verification or {}).get("items", [])
    ) or "<li>Not run</li>"
    closure_score = _format_number(
        closure.get("score") if closure is not None else None,
        3,
    )
    closure_status = (
        str(bool(closure.get("closed"))).lower()
        if closure is not None and isinstance(closure.get("closed"), bool)
        else "not recorded"
    )
    search_calls = _format_integer(counters.get("search_calls"))
    pages_fetched = _format_integer(counters.get("pages_fetched"))
    verification_passed = (
        str(bool(verification.get("passed"))).lower()
        if verification is not None
        and isinstance(verification.get("passed"), bool)
        else "not recorded"
    )
    verification_count = (
        str(len(verification.get("items", [])))
        if verification is not None and isinstance(verification.get("items"), list)
        else "not recorded"
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deep Research Run {_h(state['run_id'])}</title>
<style>
:root{{--ink:#15231d;--paper:#f5f1e8;--card:#fffdf7;--green:#1f6b4f;--gold:#c58a2b;--red:#a23b32}}
body{{margin:0;background:linear-gradient(135deg,#e6efe7,var(--paper) 42%,#f0dfbd);color:var(--ink);font:16px/1.55 Georgia,serif}}
main{{max-width:1180px;margin:auto;padding:40px 22px 70px}}h1{{font-size:clamp(32px,6vw,68px);line-height:1;margin:.2em 0}}
h2{{font-family:ui-monospace,monospace;text-transform:uppercase;letter-spacing:.08em;font-size:15px;color:var(--green)}}
.grid{{display:grid;grid-template-columns:repeat(12,1fr);gap:18px}}.card{{background:rgba(255,253,247,.9);border:1px solid #c9c3b6;border-radius:4px;padding:20px;box-shadow:7px 7px 0 rgba(31,107,79,.12)}}
.wide{{grid-column:span 12}}.half{{grid-column:span 6}}.third{{grid-column:span 4}}.metric{{font:700 30px ui-monospace,monospace;color:var(--green)}}
 code,.score{{font-family:ui-monospace,monospace;background:#e4ece4;padding:2px 6px}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;vertical-align:top;border-bottom:1px solid #ddd5c5;padding:9px}}blockquote{{font-size:20px;border-left:5px solid var(--gold);margin:0;padding:8px 20px;white-space:pre-wrap}}a{{color:var(--green)}}.entailed{{color:var(--green)}}.unsupported{{color:var(--red)}}.delivery{{font-family:ui-monospace,monospace;color:#6b501e}}
@media(max-width:760px){{.half,.third{{grid-column:span 12}}table{{display:block;overflow:auto}}}}
</style></head><body><main>
<p><code>{_h(state['status'])}</code> RUN {_h(state['run_id'])}</p><h1>{_h(state['question'])}</h1>
<div class="grid">
<section class="card third"><h2>Closure</h2><div class="metric">{closure_score}</div><p>closed: {closure_status}</p></section>
<section class="card third"><h2>Search</h2><div class="metric">{search_calls}</div><p>{pages_fetched} page attempts</p></section>
<section class="card third"><h2>Citations</h2><div class="metric">{verification_passed}</div><p>{verification_count} checked claim units · contract check, not fact certification</p></section>
<section class="card wide"><h2>Final answer</h2><p class="delivery">{_h(delivery.get('label') or '最终交付等级未记录')}</p><blockquote>{_h(state.get('draft_answer') or 'No final delivery was recorded.')}</blockquote></section>
<section class="card half"><h2>Answer slots</h2><ul>{slot_items}</ul></section>
<section class="card half"><h2>Queries</h2><ol>{query_items}</ol></section>
<section class="card wide"><h2>Evidence ledger</h2><table><thead><tr><th>ID</th><th>Slot</th><th>Claim</th><th>Quote</th><th>Source</th><th>Score</th></tr></thead><tbody>{evidence_rows}</tbody></table></section>
<section class="card half"><h2>Verification</h2><ul>{verification_items}</ul></section>
<section class="card half"><h2>Failures and recovery</h2><ul>{failure_items}</ul></section>
</div></main></body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")
    return output_path


def _evidence_row(item: dict) -> str:
    return (
        f'<tr><td><code>{_h(item["id"])}</code></td><td>{_h(item["slot_id"])}</td>'
        f'<td>{_h(item["claim"])}</td><td>{_h(item["quote"])}</td>'
        f'<td><a href="{_h(item["source_url"])}">{_h(item["source_title"])}</a></td>'
        f'<td>{_format_number(item.get("reliability"), 2)}</td></tr>'
    )


def _format_number(value: object, digits: int) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "not recorded"
    return f"{float(value):.{digits}f}"


def _format_integer(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return "not recorded"
    return str(value)


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)
