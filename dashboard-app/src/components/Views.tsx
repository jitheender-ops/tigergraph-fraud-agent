import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';

/* ------------------------------------------------------------------ waterfall */

type Signal = {
  name: string; weight: number; running: number;
  source: string; claim: string; withdrawn: boolean;
};

/** The probability, decomposed.
 *
 *  fraud_probability is a logistic over summed log-odds, and a number you cannot
 *  decompose is a number nobody can argue with. Each row is one signal's contribution
 *  and the probability after it, so an analyst can see which premise is carrying the
 *  case — and, after a challenge, watch the bar that was carrying it disappear.
 */
export function Waterfall({ signals, final }: { signals: Signal[]; final: number }) {
  const scored = signals.filter((s) => s.weight !== 0 || !s.name.startsWith('doc:'));
  const span = Math.max(1.5, ...scored.map((s) => Math.abs(s.weight))) * 1.15;
  const zero = 50;                                  // percent; the axis sits centre

  return (
    <div className="border border-line bg-white p-6">
      <div className="flex justify-between items-baseline mb-5">
        <div className="text-[10px] font-bold uppercase tracking-widest text-muted">
          Log-odds contributions
        </div>
        <div className="font-mono text-xs text-muted">
          weight · running probability
        </div>
      </div>

      {scored.map((s) => {
        const pct = (Math.abs(s.weight) / (span * 2)) * 100;
        const neg = s.weight < 0;
        return (
          // Name above the bar rather than beside it: a fixed label column squeezes the
          // bars to nothing on a narrow screen, and the bars are the whole point.
          <div key={s.name} title={s.claim}
            className={`py-2 ${s.withdrawn ? 'opacity-40' : ''}`}>
            <div className="flex items-baseline gap-3 mb-1">
              <span className={`font-mono text-[11px] text-ink truncate
                ${s.withdrawn ? 'line-through' : ''}`}>
                {s.name.replace(/^doc:/, '').replace(/::/g, ' ')}
              </span>
              <span className={`ml-auto font-mono text-[11px] tabular-nums
                ${s.weight === 0 ? 'text-muted' : neg ? 'text-emerald-700' : 'text-crimson'}`}>
                {s.weight > 0 ? '+' : ''}{s.weight.toFixed(2)}
              </span>
              <span className="font-mono text-[11px] tabular-nums text-muted w-9 text-right">
                {s.running.toFixed(2)}
              </span>
            </div>
            <div className="relative h-3">
              <div className="absolute inset-y-0 border-l border-line" style={{ left: `${zero}%` }} />
              {s.weight !== 0 && (
                <motion.div
                  initial={{ width: 0 }} animate={{ width: `${pct}%` }}
                  transition={{ duration: 0.4, ease: 'easeOut' }}
                  className={`absolute top-0 h-3 ${neg ? 'bg-emerald-600' : 'bg-crimson'}`}
                  style={neg ? { right: `${100 - zero}%` } : { left: `${zero}%` }}
                />
              )}
            </div>
          </div>
        );
      })}

      <div className="flex items-baseline pt-3 mt-2 border-t border-ink">
        <span className="font-mono text-[11px] font-bold uppercase tracking-widest">Assessed</span>
        <span className="ml-auto font-mono text-sm font-bold tabular-nums">{final.toFixed(2)}</span>
      </div>
      <p className="text-xs text-muted mt-4 leading-relaxed">
        Green is exculpatory. Every weight is a log-likelihood ratio measured on the bank's
        5,565 closed cases — a new device and a large outlier both point <i>away</i> from fraud
        on this book. Signals at 0.00 are named because policy requires it and weighted at
        nothing because they measured at nothing.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------- network */

type Node = { id: string; kind: string; label: string; center?: boolean };
type Net = { nodes: Node[]; edges: { from: string; to: string; kind: string }[];
             ring_size: number | null; ring_cap: number };

const KIND_FILL: Record<string, string> = {
  card: '#c8102e', customer: '#111111', device: '#b45309',
  closed_case: '#047857',
};

/** The ego-network the investigation walked.
 *
 *  Laid out radially rather than by a force simulation: the rings mean something (one
 *  hop, two hops), a force layout would move them every render, and an analyst comparing
 *  two cases needs the same shape to mean the same thing.
 */
export function Network({ caseId, onPickCase }: {
  caseId: string; onPickCase: (id: string) => void;
}) {
  const [net, setNet] = useState<Net | null>(null);
  useEffect(() => {
    setNet(null);
    fetch(`/api/case/${caseId}/network`).then((r) => r.json()).then(setNet).catch(() => {});
  }, [caseId]);
  if (!net) return <div className="h-64 grid place-items-center text-muted font-mono text-xs">…</div>;

  const W = 720, H = 420, cx = W / 2, cy = H / 2;
  const center = net.nodes.find((n) => n.center) ?? net.nodes[0];
  const inner = net.nodes.filter((n) => n.kind === 'device' || n.kind === 'customer');
  const outer = net.nodes.filter((n) => !n.center && !inner.includes(n));
  const place = (list: Node[], r: number) => {
    const m = new Map<string, [number, number]>();
    list.forEach((n, i) => {
      const a = (i / Math.max(list.length, 1)) * Math.PI * 2 - Math.PI / 2;
      m.set(n.id, [cx + r * Math.cos(a), cy + r * Math.sin(a) * 0.72]);
    });
    return m;
  };
  const pos = new Map<string, [number, number]>([[center.id, [cx, cy]]]);
  place(inner, 110).forEach((v, k) => pos.set(k, v));
  place(outer, 185).forEach((v, k) => pos.set(k, v));

  return (
    <div className="border border-line bg-white p-4">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
        {net.edges.map((e, i) => {
          const a = pos.get(e.from), b = pos.get(e.to);
          if (!a || !b) return null;
          return <line key={i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]}
            stroke="#d6d6d6" strokeWidth={1.2} />;
        })}
        {net.nodes.map((n) => {
          const p = pos.get(n.id);
          if (!p) return null;
          const r = n.center ? 13 : n.kind === 'device' ? 10 : 7;
          return (
            <g key={n.id} onClick={() => n.kind === 'closed_case' && onPickCase(n.id)}
              className={n.kind === 'closed_case' ? 'cursor-pointer' : ''}>
              <circle cx={p[0]} cy={p[1]} r={r} fill={KIND_FILL[n.kind] ?? '#767676'}
                stroke="#fff" strokeWidth={2} />
              <text x={p[0]} y={p[1] + r + 12} textAnchor="middle"
                fontSize={10} fontFamily="JetBrains Mono, monospace"
                fill={n.center ? '#111' : '#767676'}>
                {n.label.length > 22 ? `${n.label.slice(0, 21)}…` : n.label}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="flex flex-wrap gap-4 items-center text-[10px] font-mono uppercase
                      tracking-widest text-muted pt-2 border-t border-line">
        {Object.entries(KIND_FILL).map(([k, c]) => (
          <span key={k} className="flex items-center gap-1.5">
            <i className="w-2.5 h-2.5 rounded-full inline-block" style={{ background: c }} />
            {k.replace('_', ' ')}
          </span>
        ))}
        {net.ring_size != null && (
          <span className="ml-auto normal-case tracking-normal">
            device-sharing component: {net.ring_size.toLocaleString()} cards at a cap of {net.ring_cap}
          </span>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- timeline */

type Txn = {
  txn_id: string; ts: string; amount: number; channel: string;
  in_episode: boolean; anchor: boolean;
};

/** The episode as a shape.
 *
 *  Card testing is three authorisations under $5 and then a purchase; that is something
 *  you see, not something you read off 25 identifiers. Amount is on a log scale because
 *  the whole tell is $3 next to $259.
 */
export function Timeline({ caseId }: { caseId: string }) {
  const [txns, setTxns] = useState<Txn[] | null>(null);
  useEffect(() => {
    setTxns(null);
    fetch(`/api/case/${caseId}/episode`).then((r) => r.json())
      .then((d) => setTxns(d.txns)).catch(() => {});
  }, [caseId]);
  if (!txns) return <div className="h-40 grid place-items-center text-muted font-mono text-xs">…</div>;
  if (!txns.length) return <div className="text-muted text-sm">No transactions in the window.</div>;

  const W = 720, H = 180, padX = 40, padY = 24;
  const ts = txns.map((t) => new Date(t.ts).getTime());
  const [t0, t1] = [Math.min(...ts), Math.max(...ts)];
  const amts = txns.map((t) => Math.log10(Math.max(t.amount, 1)));
  const [a0, a1] = [Math.min(...amts), Math.max(...amts)];
  const x = (t: number) => padX + ((t - t0) / Math.max(t1 - t0, 1)) * (W - padX * 2);
  const y = (a: number) => H - padY - ((a - a0) / Math.max(a1 - a0, 0.01)) * (H - padY * 2);

  return (
    <div className="border border-line bg-white p-4">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
        <line x1={padX} y1={H - padY} x2={W - padX} y2={H - padY} stroke="#e5e5e5" />
        {[5, 100].map((v) => (
          <g key={v}>
            <line x1={padX} y1={y(Math.log10(v))} x2={W - padX} y2={y(Math.log10(v))}
              stroke="#f0f0f0" strokeDasharray="3 3" />
            <text x={4} y={y(Math.log10(v)) + 3} fontSize={9}
              fontFamily="JetBrains Mono, monospace" fill="#767676">${v}</text>
          </g>
        ))}
        {txns.map((t) => (
          <circle key={t.txn_id} cx={x(new Date(t.ts).getTime())}
            cy={y(Math.log10(Math.max(t.amount, 1)))}
            r={t.anchor ? 6 : t.in_episode ? 4 : 2.5}
            fill={t.anchor ? '#111111' : t.in_episode ? '#c8102e' : '#d6d6d6'}
            stroke={t.anchor ? '#c8102e' : 'none'} strokeWidth={2}>
            <title>{`${t.ts} · $${t.amount.toFixed(2)} · ${t.channel}${t.anchor ? ' · flagged' : ''}`}</title>
          </circle>
        ))}
        <text x={padX} y={H - 6} fontSize={9} fontFamily="JetBrains Mono, monospace" fill="#767676">
          {new Date(t0).toISOString().slice(0, 16).replace('T', ' ')}
        </text>
        <text x={W - padX} y={H - 6} textAnchor="end" fontSize={9}
          fontFamily="JetBrains Mono, monospace" fill="#767676">
          {new Date(t1).toISOString().slice(0, 16).replace('T', ' ')}
        </text>
      </svg>
      <div className="flex gap-5 text-[10px] font-mono uppercase tracking-widest text-muted
                      pt-2 border-t border-line">
        <span className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-full bg-ink inline-block" /> flagged</span>
        <span className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-full bg-crimson inline-block" /> in the episode</span>
        <span className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-full bg-line inline-block" /> other activity</span>
        <span className="ml-auto normal-case tracking-normal">log scale — the tell is $3 next to $259</span>
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- prior case */

/** What a retrieved closed case actually said. The chips under similar_prior_cases are
 *  the bank's own finished investigations; this is the analyst's note from one. */
export function PriorCase({ id, onClose }: { id: string; onClose: () => void }) {
  const [row, setRow] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    fetch(`/api/closed/${id}`).then((r) => r.ok ? r.json() : Promise.reject(r.status))
      .then(setRow).catch(() => setErr(`No closed case ${id} in the graph.`));
  }, [id]);

  return (
    <div className="fixed inset-0 bg-ink/40 grid place-items-center z-50 p-8" onClick={onClose}>
      <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-white border border-line max-w-2xl w-full p-8 shadow-2xl max-h-[80vh] overflow-y-auto">
        <div className="flex justify-between items-baseline mb-6">
          <h4 className="font-serif text-3xl font-bold">{id}</h4>
          <button onClick={onClose}
            className="font-mono text-xs uppercase tracking-widest text-muted hover:text-ink">
            close
          </button>
        </div>
        {err && <div className="text-crimson text-sm">{err}</div>}
        {!row && !err && <div className="text-muted font-mono text-xs">…</div>}
        {row && (
          <>
            <div className="grid grid-cols-2 gap-px bg-line border border-line mb-6">
              {[['Outcome', row.outcome], ['Pattern', row.pattern],
                ['Exposure', `$${Number(row.exposure_usd ?? 0).toLocaleString()}`],
                ['Transactions', row.n_txns], ['Opened', String(row.opened_at).slice(0, 10)],
                ['Report filed', String(row.report_filed)]].map(([k, v]) => (
                <div key={String(k)} className="bg-white p-4">
                  <div className="text-[10px] font-bold uppercase tracking-widest text-muted mb-1">{k}</div>
                  <div className="font-mono text-sm">{String(v)}</div>
                </div>
              ))}
            </div>
            <div className="text-[10px] font-bold uppercase tracking-widest text-muted mb-2">
              Actions taken
            </div>
            <div className="flex flex-wrap gap-2 mb-6">
              {String(row.actions_taken || '').split('|').filter(Boolean).map((a) => (
                <span key={a} className="font-mono text-[11px] border border-line px-2 py-1">{a}</span>
              ))}
            </div>
            <div className="text-[10px] font-bold uppercase tracking-widest text-muted mb-2">
              Analyst's note
            </div>
            <p className="font-serif text-lg leading-relaxed text-ink/80">{row.analyst_notes}</p>
          </>
        )}
      </motion.div>
    </div>
  );
}
