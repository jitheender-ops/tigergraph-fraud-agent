import { useState } from 'react';
import { Check, X, Gavel, ScanLine, Loader2 } from 'lucide-react';
import { api, ACTIONS } from '../api';

/** Say what actually happened.
 *
 *  On the DuckDB mirror there is no graph to write to, and a console that reports a
 *  ClosedCase vertex it did not create is doing the same thing the answer files used to
 *  do when `written_to_graph` was hardcoded true.
 */
const wrote = (r: { closed_case_id: string; written_to_graph: boolean }) =>
  r.written_to_graph
    ? `Written to the graph as ${r.closed_case_id}, where the next investigation on this card retrieves it.`
    : `Recorded as ${r.closed_case_id}, but NOT written to a graph — this console is on the DuckDB mirror.`;

const ROUTE_STYLE: Record<string, string> = {
  auto: 'bg-emerald-100 text-emerald-800',
  L1: 'bg-amber-100 text-amber-800',
  L2: 'bg-crimson/10 text-crimson',
};

export function RouteChip({ route }: { route: string }) {
  return (
    <span className={`text-[10px] font-mono font-bold uppercase tracking-widest px-2 py-1 ${ROUTE_STYLE[route] ?? 'bg-line'}`}>
      {route}
    </span>
  );
}

/** Approve, override, close, blacklist — the four things that leave a record. */
export function Decide({ caseId, device, closed, onEvent }: {
  caseId: string; device?: string; closed: string | null;
  onEvent: (note: string, closed?: string) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [override, setOverride] = useState('');
  const [note, setNote] = useState('');

  const run = async (label: string, fn: () => Promise<any>, describe: (r: any) => string,
                     closedAs?: string) => {
    setBusy(label);
    try {
      onEvent(describe(await fn()), closedAs);
    } catch (e) {
      onEvent(String(e).slice(0, 200));
    } finally {
      setBusy(null);
    }
  };

  if (closed) {
    return (
      <div className="border border-line bg-paper p-6 font-mono text-xs uppercase tracking-widest text-muted">
        Case closed as {closed.replace('_', ' ')} · written to the graph as a ClosedCase,
        where the next investigation on this card retrieves it
      </div>
    );
  }

  return (
    <div className="border border-line bg-white p-6 shadow-editorial">
      <div className="text-[10px] font-bold uppercase tracking-widest text-muted mb-4">
        Decision
      </div>

      <div className="flex flex-wrap gap-2 mb-5">
        <Primary busy={busy === 'approve'} icon={<Check className="w-4 h-4" />}
          onClick={() => run('approve', () => api.decide(caseId, 'approve'),
            (r) => `Approved. Executed ${r.executed.length} auto action(s); ` +
                   `${r.pending_approval.length} awaiting human approval` +
                   (r.pending_approval.length
                     ? ` (${r.pending_approval.map((a: any) => `${a.action} → ${a.route}`).join(', ')})`
                     : ''))}>
          Approve recommended
        </Primary>
        <Primary busy={busy === 'fraud'} tone="crimson" icon={<Gavel className="w-4 h-4" />}
          onClick={() => run('fraud', () => api.close(caseId, 'confirmed_fraud', note),
            (r) => `Closed as confirmed fraud. ${wrote(r)}`,
            'confirmed_fraud')}>
          Close: confirmed fraud
        </Primary>
        <Primary busy={busy === 'cleared'} tone="muted" icon={<X className="w-4 h-4" />}
          onClick={() => run('cleared', () => api.close(caseId, 'cleared', note),
            (r) => `Closed as a false positive. ${wrote(r)}`,
            'cleared')}>
          Close: false positive
        </Primary>
        {device && (
          <Primary busy={busy === 'bl'} tone="muted" icon={<ScanLine className="w-4 h-4" />}
            onClick={() => run('bl', () => api.blacklist(device, note || 'Blacklisted from the console.'),
              (r) => `Device blacklisted. ${wrote(r)} ` +
                     `${r.cards_now_flagged.length} card(s) on that profile now reach a confirmed-fraud case.`)}>
            Blacklist device
          </Primary>
        )}
      </div>

      <div className="flex gap-2 items-stretch">
        <select value={override} onChange={(e) => setOverride(e.target.value)}
          className="border border-line px-3 py-2 font-mono text-xs bg-paper focus:outline-none focus:border-crimson">
          <option value="">Override with…</option>
          {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <input value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="Analyst note (recorded on the case)"
          className="flex-1 border border-line px-3 py-2 text-sm bg-paper focus:outline-none focus:border-crimson" />
        <button disabled={!override || busy !== null}
          onClick={() => run('override', () => api.decide(caseId, 'override', override, note),
            (r) => `Overrode to ${r.action}, which the policy routes ${r.route}.`)}
          className="px-4 border border-ink text-xs font-bold uppercase tracking-widest
                     hover:bg-ink hover:text-paper transition-colors disabled:opacity-30">
          {busy === 'override' ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Override'}
        </button>
      </div>
      <p className="text-xs text-muted mt-3">
        An override is still routed by the policy table — overriding to BLOCK_ALL_CARDS
        still returns the L2 approval it demands.
      </p>
    </div>
  );
}

/** Trigger an investigation live: what a model alert, a cardholder call or an analyst
 *  would actually do. Every other case in the console arrived from a batch run. */
export function OpenCase({ onOpened }: { onOpened: (caseId: string) => void }) {
  const [card, setCard] = useState('');
  const [txn, setTxn] = useState('');
  const [kind, setKind] = useState('risk_score');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const submit = async () => {
    setBusy(true); setErr('');
    try {
      onOpened((await api.open(card.trim(), Number(txn), kind, text)).case_id);
      setCard(''); setTxn(''); setText('');
    } catch (e) {
      setErr(String(e).slice(0, 200));
    } finally {
      setBusy(false);
    }
  };
  const field = 'border border-line px-2 py-1 text-sm bg-white focus:outline-none focus:border-crimson';
  return (
    <div className="p-6 border-b border-line bg-paper space-y-2">
      <div className="text-[10px] font-bold uppercase tracking-widest text-muted">Open a case</div>
      <input value={card} onChange={(e) => setCard(e.target.value)} placeholder="card id, e.g. C04172-K2" className={`${field} w-full`} />
      <input value={txn} onChange={(e) => setTxn(e.target.value.replace(/\D/g, ''))} placeholder="flagged transaction id" inputMode="numeric" className={`${field} w-full`} />
      <select value={kind} onChange={(e) => setKind(e.target.value)} className={`${field} w-full font-mono text-xs`}>
        <option value="risk_score">risk_score</option>
        <option value="customer_report">customer_report</option>
        <option value="analyst_request">analyst_request</option>
      </select>
      <input value={text} onChange={(e) => setText(e.target.value)} placeholder="trigger note (optional)" className={`${field} w-full`} />
      <Primary busy={busy} icon={<ScanLine className="w-4 h-4" />} onClick={() => { if (card && txn) submit(); }}>
        Investigate
      </Primary>
      {err && <p className="text-xs text-crimson">{err}</p>}
    </div>
  );
}

function Primary({ children, onClick, icon, busy, tone = 'ink' }: {
  children: React.ReactNode; onClick: () => void; icon: React.ReactNode;
  busy: boolean; tone?: 'ink' | 'crimson' | 'muted';
}) {
  const bg = tone === 'crimson' ? 'bg-crimson text-white hover:bg-ink'
    : tone === 'muted' ? 'border border-line text-muted hover:text-ink hover:border-ink'
      : 'bg-ink text-paper hover:bg-crimson';
  return (
    <button onClick={onClick} disabled={busy}
      className={`flex items-center gap-2 px-4 py-2 text-xs font-bold uppercase tracking-widest
                  transition-colors disabled:opacity-40 ${bg}`}>
      {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : icon}
      {children}
    </button>
  );
}
