#!/usr/bin/env python
"""Generate a self-contained analyst dashboard from cases/*.json.

One HTML file with the case data embedded, so it opens from the filesystem with no
server and no build step:  uv run python ui/build.py && open ui/dashboard.html
"""
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
cases = [json.loads(p.read_text()) for p in (ROOT / (sys.argv[1] if len(sys.argv) > 1 else "cases")).glob("*.json")]
cases.sort(key=lambda c: c["case_id"])
pack = {c["case_id"]: c for c in cases}

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fraud Investigation Console</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#fff; --ink:#14181f; --muted:#5d6672; --line:#e3e6ea;
  --fraud:#b3261e; --legit:#1a7f45; --unsure:#9a6400; --accent:#1c4fd8;
  --chip:#eef1f5; --auto:#1a7f45; --l1:#9a6400; --l2:#b3261e;
}
:root:not([data-theme=light]) @media (prefers-color-scheme:dark){}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#0e1116; --panel:#161b22; --ink:#e7eaee; --muted:#9aa4b2; --line:#252c36;
  --fraud:#ff6b5e; --legit:#4ad584; --unsure:#e5b13a; --accent:#7aa2ff; --chip:#1d232c;
  --auto:#4ad584; --l1:#e5b13a; --l2:#ff6b5e;
}}
:root[data-theme=dark]{
  --bg:#0e1116; --panel:#161b22; --ink:#e7eaee; --muted:#9aa4b2; --line:#252c36;
  --fraud:#ff6b5e; --legit:#4ad584; --unsure:#e5b13a; --accent:#7aa2ff; --chip:#1d232c;
  --auto:#4ad584; --l1:#e5b13a; --l2:#ff6b5e;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:20px 16px 8px;max-width:1280px;margin:0 auto}
h1{font-size:19px;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px}
.wrap{max-width:1280px;margin:0 auto;padding:12px 16px 64px;display:grid;
 grid-template-columns:300px 1fr;gap:18px;align-items:start}
@media(max-width:860px){.wrap{grid-template-columns:1fr}}
.list{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;
 position:sticky;top:12px;max-height:calc(100vh - 40px);overflow-y:auto}
@media(max-width:860px){.list{position:static;max-height:none}}
.row{padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;display:flex;
 gap:8px;align-items:center}
.row:last-child{border-bottom:0}
.row:hover{background:var(--chip)}
.row.on{background:var(--chip);box-shadow:inset 3px 0 0 var(--accent)}
.row b{font-size:13px;font-variant-numeric:tabular-nums}
.row .v{margin-left:auto;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.fraud{color:var(--fraud)} .legit{color:var(--legit)} .unsure{color:var(--unsure)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
 margin:0 0 12px;font-weight:600}
.hd{display:flex;flex-wrap:wrap;gap:14px;align-items:baseline}
.hd .id{font-size:22px;font-weight:650;letter-spacing:-.02em}
.trig{color:var(--muted);font-size:13px;flex-basis:100%}
.meter{height:7px;background:var(--chip);border-radius:99px;overflow:hidden;margin:10px 0 4px}
.meter i{display:block;height:100%;border-radius:99px}
.pl{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);
 font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:14px}
.stat{background:var(--chip);border-radius:9px;padding:9px 11px}
.stat .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.stat .v{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:1px}
ol.steps{margin:0;padding-left:18px;color:var(--muted);font-size:13px}
ol.steps li{margin:3px 0}
.ev{border-left:2px solid var(--line);padding:2px 0 2px 12px;margin:0 0 13px}
.ev .c{font-size:14px}
.ev .m{font-size:11.5px;color:var(--muted);margin-top:3px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
.tag{display:inline-block;font-size:10px;font-weight:700;text-transform:uppercase;
 letter-spacing:.05em;padding:1px 6px;border-radius:4px;background:var(--chip);color:var(--muted);margin-right:6px}
/* Evidence sources read differently and should look different: the graph observed it,
   a document governs it, the cardholder said it, someone outside the bank supplied it. */
.tag.s-graph{background:color-mix(in srgb,var(--accent) 16%,transparent);color:var(--accent)}
.tag.s-document{background:color-mix(in srgb,var(--legit) 16%,transparent);color:var(--legit)}
.tag.s-customer{background:color-mix(in srgb,var(--unsure) 20%,transparent);color:var(--unsure)}
.tag.s-external{background:color-mix(in srgb,var(--fraud) 14%,transparent);color:var(--fraud)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:720px){.cols{grid-template-columns:1fr}}
.act{display:flex;gap:9px;align-items:flex-start;padding:9px 0;border-bottom:1px dashed var(--line)}
.act:last-child{border-bottom:0}
.act .n{font-weight:600;font-size:13.5px}
.act .r{font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;margin-left:auto;flex:none}
.r.auto{color:var(--auto);border:1px solid var(--auto)}
.r.L1{color:var(--l1);border:1px solid var(--l1)}
.r.L2{color:var(--l2);border:1px solid var(--l2)}
.act .why{font-size:12.5px;color:var(--muted);margin-top:2px}
.chg{background:var(--chip);border-radius:9px;padding:11px 13px;font-size:13.5px;margin-top:12px}
.nar{white-space:pre-wrap;font-size:14px;line-height:1.65}
.pill{display:inline-block;background:var(--chip);border-radius:6px;padding:2px 8px;
 font-size:12px;margin:0 5px 5px 0;font-family:ui-monospace,Menlo,monospace}
.none{color:var(--muted);font-size:13.5px}
button.theme{position:fixed;right:14px;top:14px;background:var(--panel);color:var(--muted);
 border:1px solid var(--line);border-radius:8px;padding:6px 10px;cursor:pointer;font-size:12px}
</style></head><body>
<button class="theme" onclick="var r=document.documentElement,d=r.getAttribute('data-theme')==='dark';r.setAttribute('data-theme',d?'light':'dark')">theme</button>
<header><h1>Fraud Investigation Console</h1>
<div class="sub">TigerGraph agentic investigation &middot; <span id="n"></span> cases from the Hacker House Goa case pack</div></header>
<div class="wrap"><div class="list" id="list"></div><div id="detail"></div></div>
<script>
const DATA = __DATA__;
const ids = Object.keys(DATA).sort();
const vc = v => v==='fraud'?'fraud':v==='legitimate'?'legit':'unsure';
const money = n => '$'+Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const esc = s => String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
document.getElementById('n').textContent = ids.length;

function acts(list){ return list.map(a=>`<div class="act"><div><div class="n">${esc(a.action)}</div>
  <div class="why">${esc(a.reason)}</div></div><span class="r ${a.route}">${a.route}</span></div>`).join('') }

function render(id){
  const d = DATA[id], c = d.case, s = d.sar;
  document.querySelectorAll('.row').forEach(r=>r.classList.toggle('on', r.dataset.id===id));
  const pct = Math.round(c.fraud_probability*100);
  const col = `var(--${vc(c.verdict)})`;
  document.getElementById('detail').innerHTML = `
  <div class="card"><div class="hd"><span class="id">${esc(id)}</span>
    <span class="${vc(c.verdict)}" style="font-weight:650">${esc(c.verdict).toUpperCase()}</span>
    <span class="none">${esc(c.status)}</span>
    <div class="trig">${esc(d._trigger||'')}</div></div>
    <div class="meter"><i style="width:${pct}%;background:${col}"></i></div>
    <div class="pl"><span>fraud probability ${c.fraud_probability.toFixed(2)}</span>
      <span>${esc(c.pattern)}</span></div>
    <div class="grid">
      <div class="stat"><div class="k">Exposure</div><div class="v">${money(c.exposure_usd)}</div></div>
      <div class="stat"><div class="k">Transactions</div><div class="v">${c.affected_txn_ids.length}</div></div>
      <div class="stat"><div class="k">Connected cards</div><div class="v">${c.connected_card_ids.length}</div></div>
      <div class="stat"><div class="k">Prior cases used</div><div class="v">${c.similar_prior_cases.length}</div></div>
      <div class="stat"><div class="k">Graph calls</div><div class="v">${d.tool_calls}</div></div>
      <div class="stat"><div class="k">Latency</div><div class="v">${d.latency_s}s</div></div>
    </div></div>

  <div class="card"><h2>Case summary</h2><div>${esc(c.summary)}</div>
    ${c.pattern_description?`<div class="chg"><b>Undocumented pattern.</b> ${esc(c.pattern_description)}</div>`:''}
  </div>

  <div class="card"><h2>Evidence (${c.evidence.length})</h2>
    ${c.evidence.map(e=>`<div class="ev"><div class="c"><span class="tag s-${esc(e.source)}">${esc(e.source)}</span>${esc(e.claim)}</div>
      <div class="m">${esc(e.ref)}${e.entity_ids.length?' &middot; '+e.entity_ids.map(esc).join(', '):''}</div></div>`).join('')}
  </div>

  ${d.evidence_requests.length?`<div class="card"><h2>Evidence requested</h2>
    ${d.evidence_requests.map(q=>`<div class="ev"><div class="c"><span class="tag">${esc(q.type)}</span>after step ${q.asked_after_step}</div>
      <div style="font-size:13.5px;color:var(--muted);margin-top:4px">${esc(q.assumed_response)}</div></div>`).join('')}
  </div>`:''}

  <div class="card"><h2>Next best action</h2><div class="cols">
    <div><div class="k" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Before evidence</div>${acts(d.next_best_actions.initial)}</div>
    <div><div class="k" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">After evidence</div>${acts(d.next_best_actions.final)}</div>
  </div><div class="chg"><b>What changed:</b> ${esc(d.next_best_actions.what_changed)}</div></div>

  <div class="card"><h2>Suspicious activity report</h2>
    ${s.file?`<div class="nar">${esc(s.narrative)}</div>
      <div style="margin-top:12px">${s.subjects.map(x=>`<span class="pill">${esc(x)}</span>`).join('')}</div>
      <div class="pl" style="margin-top:8px"><span>${money(s.total_amount_usd)}</span><span>${s.activity_dates.join(' to ')}</span></div>`
    :`<div class="none">Not filed. ${esc(s.reason)}</div>`}
  </div>

  <div class="card"><h2>Why the investigation stopped</h2><div>${esc(d.stop_reason)}</div>
    ${c.affected_txn_ids.length?`<div style="margin-top:12px"><div class="k" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Affected transactions</div>${c.affected_txn_ids.map(x=>`<span class="pill">${esc(x)}</span>`).join('')}</div>`:''}
    ${c.connected_card_ids.length?`<div style="margin-top:10px"><div class="k" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Connected cards</div>${c.connected_card_ids.map(x=>`<span class="pill">${esc(x)}</span>`).join('')}</div>`:''}
    ${c.similar_prior_cases.length?`<div style="margin-top:10px"><div class="k" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Prior cases retrieved as memory</div>${c.similar_prior_cases.map(x=>`<span class="pill">${esc(x)}</span>`).join('')}</div>`:''}
    <div style="margin-top:10px"><span class="none">Written to graph as <b>${esc(c.graph_case_id)}</b></span></div>
  </div>`;
  location.hash = id;
}

document.getElementById('list').innerHTML = ids.map(id=>{
  const c = DATA[id].case;
  return `<div class="row" data-id="${id}" onclick="render('${id}')">
    <b>${id}</b><span class="none">${c.fraud_probability.toFixed(2)}</span>
    <span class="v ${vc(c.verdict)}">${c.verdict}</span></div>`;
}).join('');
render(location.hash.slice(1) && DATA[location.hash.slice(1)] ? location.hash.slice(1) : ids[0]);
</script></body></html>"""

# attach the trigger text for display
import duckdb
con = duckdb.connect(str(ROOT / "build/fraud.db"), read_only=True)
for cid, txt in con.sql("SELECT case_id, trigger_text FROM case_pack").fetchall():
    if cid in pack:
        pack[cid]["_trigger"] = txt

out = ROOT / "ui" / "dashboard.html"
out.write_text(HTML.replace("__DATA__", json.dumps(pack)))
print(f"{out}  ({out.stat().st_size/1024:.0f} KB, {len(pack)} cases)")
