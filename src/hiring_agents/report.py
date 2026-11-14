"""Renders a FinalRanking as a self-contained HTML page for viewing in a browser.

Standard library only: no template engine, no external CSS or JS. Every value from the
request or the LLM is escaped before it goes into the page.

The page shows real IDs (via `ref_map`), so it is re-identified output: write it only on our
side, after all LLM calls, and keep it out of git (`results/` is gitignored).
"""

from datetime import datetime
from html import escape

from .schemas import FinalRanking, HiringRequest

CSS = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1c1f24; --muted:#636b76; --line:#e2e5ea;
        --accent:#2f6fde; --track:#e8ebf0; --warn-bg:#fff6e0; --warn:#8a5a00;
        --bad-bg:#fdecec; --bad:#a12a2a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14161a; --card:#1d2026; --text:#e6e8eb; --muted:#9aa2ad; --line:#2d3139;
          --accent:#6f9cf0; --track:#2b2f37; --warn-bg:#3a2f14; --warn:#f0c878;
          --bad-bg:#3a1c1c; --bad:#f0a0a0; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:860px; margin:0 auto; padding:32px 16px 64px; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:15px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:32px 0 12px; }
.meta { color:var(--muted); font-size:13px; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; }
.chip { border:1px solid var(--line); border-radius:999px; padding:2px 10px; font-size:13px; background:var(--card); }
.notes { margin-top:12px; font-size:14px; color:var(--muted); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin-bottom:12px; }
.head { display:flex; align-items:center; gap:14px; }
.rank { flex:none; width:36px; height:36px; border-radius:50%; background:var(--accent); color:#fff;
        display:grid; place-items:center; font-weight:600; }
.who { flex:1; min-width:0; }
.who b { font-size:16px; }
.ref { color:var(--muted); font-size:12px; font-family:ui-monospace,Consolas,monospace; }
.total { text-align:right; }
.total b { font-size:22px; font-variant-numeric:tabular-nums; }
.total span { display:block; color:var(--muted); font-size:12px; }
.crit { margin-top:14px; }
.row { display:grid; grid-template-columns:170px 1fr 40px; gap:10px; align-items:center; font-size:14px; }
.bar { height:8px; border-radius:4px; background:var(--track); overflow:hidden; }
.bar i { display:block; height:100%; background:var(--accent); }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.row.map { grid-template-columns:1fr 1fr; font-family:ui-monospace,Consolas,monospace; font-size:13px; }
.why { color:var(--muted); font-size:13px; margin:2px 0 10px 180px; }
.row span:first-child { overflow-wrap:anywhere; }
summary { cursor:pointer; font-size:14px; }
.box { border-radius:8px; padding:10px 14px; margin-bottom:8px; font-size:14px; }
.excluded { background:var(--bad-bg); color:var(--bad); }
.warning { background:var(--warn-bg); color:var(--warn); }
.empty { color:var(--muted); font-size:14px; }
@media (max-width:560px) {
  .row { grid-template-columns:1fr 40px; }
  .row .bar { grid-column:1 / -1; grid-row:2; }
  .why { margin-left:0; }
}
"""


def render_html(final: FinalRanking, ref_map: dict[str, str], request: HiringRequest) -> str:
    """Build the results page.

    Args:
        final: Output of `rank_candidates`.
        ref_map: ref -> real ID, used to show UCIDs next to each ref.
        request: The original request, for the weights and notes shown in the header.

    Returns:
        A complete HTML document as a string.
    """
    total_weight = sum(w for w in request.criteria.values() if w > 0) or 1
    chips = "".join(
        f'<span class="chip">{escape(cid)} · {w / total_weight:.0%}</span>'
        for cid, w in request.criteria.items() if w > 0
    )
    notes = f'<p class="notes">{escape(request.notes)}</p>' if request.notes else ""
    if final.reviewed and final.review_summary:
        notes += f'<p class="notes"><b>Manager review:</b> {escape(final.review_summary)}</p>'
    if final.tiebreak_fields:
        notes += ('<p class="notes">Other information (no criterion uses it; only breaks ties): '
                  f'{escape(", ".join(final.tiebreak_fields))}</p>')
    if request.documents:
        notes += f'<p class="notes">Drafted from: {escape(", ".join(request.documents))}</p>'
    if request.sessions:
        notes += ('<p class="notes">Sessions to cover (not used for ranking yet): '
                  + escape("; ".join(f"{s.label} {s.day} {s.start}-{s.end}" for s in request.sessions)) + "</p>")

    cards = []
    for i, c in enumerate(final.ranking, 1):
        rows = []
        for cid, score in c.per_criterion.items():
            rows.append(
                f'<div class="row"><span>{escape(cid)}</span>'
                f'<div class="bar"><i style="width:{score * 10}%"></i></div>'
                f'<span class="num">{score}/10</span></div>'
                f'<p class="why">{escape(c.rationales.get(cid, ""))}</p>'
            )
        weighted = c.weighted_score if c.weighted_score is not None else c.total_score
        label = (f"weighted {weighted:.2f} {c.adjustment:+.2f}" if c.adjustment
                 else ("final score" if final.reviewed else "weighted score"))
        review = ""
        if c.justification:
            review += f'<p class="why"><b>Manager:</b> {escape(c.justification)}</p>'
        if c.adjustment:
            review += f'<p class="why"><b>Adjusted {c.adjustment:+.2f}:</b> {escape(c.adjustment_reason)}</p>'
        if c.tiebreak_reason:
            review += f'<p class="why"><b>Tie broken by other information:</b> {escape(c.tiebreak_reason)}</p>'
        cards.append(
            f'<div class="card"><div class="head"><div class="rank">{i}</div>'
            f'<div class="who"><b>UCID {escape(ref_map.get(c.candidate_ref, "?"))}</b>'
            f'<div class="ref">{escape(c.candidate_ref)}</div></div>'
            f'<div class="total"><b>{c.total_score:.2f}</b><span>{escape(label)}</span></div></div>'
            f'{review}<div class="crit">{"".join(rows)}</div></div>'
        )

    excluded = "".join(
        f'<div class="box excluded"><b>UCID {escape(ref_map.get(e.candidate_ref, "?"))}</b>: '
        f'{escape(e.reason)}</div>'
        for e in final.excluded
    ) or '<p class="empty">None</p>'
    criteria = "".join(
        f'<div class="card"><div class="head"><div class="who"><b>{escape(c.name)}</b> '
        f'<span class="chip">{c.source}</span><div class="ref">{escape(c.id)}</div></div>'
        f'<div class="total"><b>{c.weight:.0%}</b><span>of total weight</span></div></div>'
        f'<p class="meta">Fields: {escape(", ".join(c.fields))}</p>'
        + (f'<p class="meta">Instructions: {escape(request.instructions[c.id])}</p>'
           if request.instructions.get(c.id) else "")
        + '</div>'
        for c in final.criteria
    )
    if final.skipped_fields:
        criteria += (
            f'<details class="card"><summary>{len(final.skipped_fields)} fields not ranked on</summary>'
            + "".join(f'<p class="meta"><b>{escape(s.field)}</b> ({escape(s.use.replace("_", " "))}): '
                      f'{escape(s.reason)}</p>' for s in final.skipped_fields)
            + "</details>"
        )

    intake = '<p class="empty">Not recorded.</p>'
    if final.intake:
        mapped = "".join(
            f'<div class="row map"><span>{escape(col)}</span><span>→ {escape(fid)}</span></div>'
            for col, fid in final.intake.column_mapping.items()
        )
        dropped = "".join(
            f'<p class="meta"><b>{label}:</b> {escape(", ".join(cols))}</p>'
            for label, cols in [("Dropped (sensitive)", final.intake.dropped_sensitive),
                                ("Dropped (not needed)", final.intake.dropped_unused),
                                ("Dropped (unrecognized)", final.intake.dropped_unmatched),
                                ("Missing fields", final.intake.missing_fields)] if cols
        )
        intake = f'<div class="card">{mapped}{dropped}</div>'
    warnings = "".join(
        f'<div class="box warning">{escape(w)}</div>' for w in final.warnings
    ) or '<p class="empty">None, everything ran cleanly.</p>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ranking Results</title><style>{CSS}</style></head>
<body><main>
<h1>{escape(final.role)}</h1>
<div class="meta">Generated {datetime.now():%Y-%m-%d %H:%M} · top {len(final.ranking)} of {len(ref_map)} candidates</div>
<div class="chips">{chips}</div>{notes}
<h2>Ranking</h2>{"".join(cards) or '<p class="empty">No candidates ranked.</p>'}
<h2>Criteria</h2>{criteria or '<p class="empty">Not recorded.</p>'}
<h2>Excluded</h2>{excluded}
<h2>Warnings</h2>{warnings}
<h2>Data intake</h2>{intake}
</main></body></html>
"""
