import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Send, Layers, ShieldQuestion, Loader2, Undo2 } from 'lucide-react';
import { api, type Diff } from '../api';

/** What changed, in the agent's own numbers.
 *
 *  The point of showing the arithmetic is that the analyst can check it. A probability
 *  that moved because a premise was withdrawn is auditable; one the model revised is not.
 */
export function DiffBanner({ diff, note }: { diff?: Diff; note?: string }) {
  if (!diff && !note) return null;
  const rows = Object.entries(diff ?? {}).filter(([, [a, b]]) => JSON.stringify(a) !== JSON.stringify(b));
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      className="border-l-4 border-crimson bg-crimson/5 px-6 py-4 mb-8 overflow-hidden"
    >
      {note && <div className="font-serif text-lg text-ink mb-2">{note}</div>}
      {rows.length === 0 && !note && (
        <div className="font-mono text-xs uppercase tracking-widest text-muted">
          Re-investigated — nothing moved
        </div>
      )}
      {rows.map(([k, [a, b]]) => (
        <div key={k} className="flex items-baseline gap-3 font-mono text-sm py-1">
          <span className="text-muted uppercase text-[10px] tracking-widest w-36 shrink-0">
            {k.replace(/_/g, ' ')}
          </span>
          <span className="text-muted line-through">{fmt(a)}</span>
          <span className="text-muted">→</span>
          <span className="text-crimson font-bold">{fmt(b)}</span>
        </div>
      ))}
    </motion.div>
  );
}

const fmt = (v: unknown) =>
  v === null || v === undefined ? '—'
    : Array.isArray(v) ? (v.length ? v.join(', ') : 'none')
      : String(v);

/** Challenge / deepen / step-up: the three ways an analyst changes the question. */
export function Steering({ caseId, steered, onResult }: {
  caseId: string;
  steered: boolean;
  onResult: (payload: { case?: any; changed?: Diff; note?: string }) => void;
}) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState<string | null>(null);

  const run = async (label: string, fn: () => Promise<any>) => {
    setBusy(label);
    try {
      const r = await fn();
      if (r.matched && r.matched.length === 0) {
        onResult({ note: r.note ?? 'No signal in this case matches that objection.' });
      } else if (r.passed !== undefined) {
        onResult({ case: r.case, changed: r.changed,
          note: `Step-up ${r.passed ? 'completed' : 'not completed'} — simulated, and weighted as such.` });
      } else if (r.restored) {
        onResult({ case: r.case, changed: r.changed,
          note: `Restored: ${r.restored.join(', ')} — back to the agent's own assessment.` });
      } else {
        onResult({ case: r.case, changed: r.changed,
          note: r.note ?? (r.matched ? `Withdrawn: ${r.matched.join(', ')}` : undefined) });
      }
    } catch (e) {
      onResult({ note: String(e).slice(0, 200) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="border border-line bg-white p-6 mb-10 shadow-editorial">
      <div className="text-[10px] font-bold uppercase tracking-widest text-muted mb-4">
        Argue with the agent
      </div>
      <div className="flex gap-2 mb-4">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && text.trim()) run('challenge', () => api.challenge(caseId, text)); }}
          placeholder="Ignore the out-of-region flag, the customer is on holiday."
          className="flex-1 border border-line px-4 py-3 font-serif text-lg bg-paper
                     focus:outline-none focus:border-crimson transition-colors"
        />
        <button
          disabled={!text.trim() || busy !== null}
          onClick={() => run('challenge', () => api.challenge(caseId, text))}
          className="px-5 bg-ink text-paper font-bold text-xs uppercase tracking-widest
                     hover:bg-crimson transition-colors disabled:opacity-30 flex items-center gap-2"
        >
          {busy === 'challenge' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          Challenge
        </button>
      </div>
      <p className="text-xs text-muted mb-5 leading-relaxed max-w-2xl">
        The objection withdraws the premise it names. The probability is then recomputed by the
        same log-odds sum as before — the model maps your words onto a signal, it never writes
        the number.
      </p>
      <div className="flex gap-2">
        <Secondary busy={busy === 'deepen'} icon={<Layers className="w-4 h-4" />}
          onClick={() => run('deepen', () => api.deepen(caseId, 20))}>
          Look wider (cap 20)
        </Secondary>
        <Secondary busy={busy === 'stepup'} icon={<ShieldQuestion className="w-4 h-4" />}
          onClick={() => run('stepup', () => api.stepup(caseId))}>
          Send step-up auth
        </Secondary>
        {steered && (
          <Secondary busy={busy === 'reset'} icon={<Undo2 className="w-4 h-4" />}
            onClick={() => run('reset', () => api.reset(caseId))}>
            Undo steering
          </Secondary>
        )}
      </div>
    </div>
  );
}

function Secondary({ children, onClick, icon, busy }: {
  children: React.ReactNode; onClick: () => void; icon: React.ReactNode; busy: boolean;
}) {
  return (
    <button onClick={onClick} disabled={busy}
      className="flex items-center gap-2 border border-line px-4 py-2 text-xs font-bold
                 uppercase tracking-widest text-muted hover:text-ink hover:border-ink
                 transition-colors disabled:opacity-40">
      {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : icon}
      {children}
    </button>
  );
}

export { AnimatePresence };
