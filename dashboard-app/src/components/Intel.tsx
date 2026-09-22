import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { ShieldCheck, Clock } from 'lucide-react';

/** Memory that tells you something, rather than memory you can query.
 *
 *  prior_cases_for_card answers "what does the bank know about THIS card". A lookup
 *  cannot see a pattern. This walks every closed case and every case the agent opened
 *  and reports what appears in more than one — filtered to device profiles that
 *  plausibly describe a machine, because 'Windows | unknown | chrome 63.0 | unknown'
 *  recurs across 260 cases and that is a fact about Chrome.
 */
export function Intelligence({ onPickCase }: { onPickCase: (id: string) => void }) {
  const [d, setD] = useState<any>(null);
  const [min, setMin] = useState(2);
  useEffect(() => {
    setD(null);
    fetch(`/api/intelligence?min_cases=${min}`).then(r => r.json()).then(setD).catch(() => {});
  }, [min]);

  return (
    <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}
      className="p-16 max-w-5xl mx-auto">
      <h2 className="text-6xl font-serif font-black tracking-tighter mb-4 text-ink leading-none">
        What Recurs
      </h2>
      <p className="text-xl text-muted font-serif italic mb-12 max-w-2xl leading-relaxed">
        Entities that appear in more than one investigation, across every case the bank has
        closed and every case this agent has opened.
      </p>

      <div className="flex gap-2 mb-10">
        {[2, 3, 5, 10].map(n => (
          <button key={n} onClick={() => setMin(n)}
            className={`px-4 py-2 text-xs font-bold uppercase tracking-widest border transition-colors
              ${min === n ? 'bg-ink text-white border-ink' : 'border-line text-muted hover:border-ink hover:text-ink'}`}>
            {n}+ cases
          </button>
        ))}
      </div>

      {!d && <div className="text-muted font-mono text-xs">…</div>}
      {d && (
        <>
          <p className="font-mono text-xs uppercase tracking-widest text-muted mb-6">
            {d.cards} cards · {d.devices} device profiles
          </p>
          <div className="border border-line bg-white divide-y divide-line">
            {d.entities.map((r: any) => (
              <div key={r.entity} className="p-5 flex flex-wrap gap-4 items-baseline">
                <span className={`text-[10px] font-bold uppercase tracking-widest px-2 py-1 shrink-0
                  ${r.kind === 'device' ? 'bg-amber-100 text-amber-800' : 'bg-crimson/10 text-crimson'}`}>
                  {r.kind}
                </span>
                <span className="font-mono text-sm text-ink break-all flex-1 min-w-0">{r.entity}</span>
                <span className="font-mono text-2xl font-serif tabular-nums">{r.n_cases}</span>
                <span className="font-mono text-[10px] uppercase tracking-widest text-muted shrink-0">cases</span>
                <div className="w-full flex flex-wrap gap-2 pt-1">
                  {r.patterns.map((p: string) => (
                    <span key={p} className="font-mono text-[10px] text-muted">{p.replace(/_/g, ' ')}</span>
                  ))}
                  <span className="ml-auto flex flex-wrap gap-1 justify-end">
                    {r.cases.slice(0, 6).map((c: string) => (
                      <button key={c} onClick={() => onPickCase(c)}
                        className="font-mono text-[10px] border border-line px-1.5 py-0.5
                                   text-muted hover:border-ink hover:text-ink transition-colors">
                        {c}
                      </button>
                    ))}
                    {r.cases.length > 6 && (
                      <span className="font-mono text-[10px] text-muted">+{r.cases.length - 6}</span>
                    )}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </motion.div>
  );
}

/** What the case actually did to the world, and what it is still waiting on a human for.
 *  Policy section 2 lets the agent execute `auto` and only recommend the rest; until
 *  this existed that was a label on a list. */
export function Ledger({ caseId, refresh }: { caseId: string; refresh: number }) {
  const [d, setD] = useState<any>(null);
  useEffect(() => {
    fetch(`/api/case/${caseId}/ledger`).then(r => r.json()).then(setD).catch(() => {});
  }, [caseId, refresh]);
  if (!d || !d.ledger.length) {
    return (
      <p className="text-muted font-serif italic">
        Nothing executed yet. Approving the recommended set runs the <code>auto</code> actions
        and holds the rest for the approval route the policy assigns them.
      </p>
    );
  }
  return (
    <div className="border border-line bg-white divide-y divide-line">
      {d.ledger.map((r: any, i: number) => (
        <div key={i} className="p-4 flex flex-wrap gap-3 items-baseline">
          {r.status === 'executed'
            ? <ShieldCheck className="w-4 h-4 text-emerald-700 shrink-0" />
            : <Clock className="w-4 h-4 text-amber-600 shrink-0" />}
          <span className="font-mono text-sm font-bold text-ink">{r.action}</span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-muted">
            {r.system}
          </span>
          {r.reference && (
            <span className="font-mono text-[10px] text-emerald-700">{r.reference}</span>
          )}
          <span className="font-mono text-[10px] text-muted ml-auto">{r.at.split('T')[1]}</span>
          <div className="w-full text-sm text-ink/80">{r.effect}</div>
        </div>
      ))}
      <div className="p-3 font-mono text-[10px] uppercase tracking-widest text-muted">
        {d.executed} executed · {d.awaiting_approval} awaiting approval · every effect simulated
      </div>
    </div>
  );
}
