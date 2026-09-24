import { useState } from 'react';
import { ScanLine, Loader2 } from 'lucide-react';
import { api } from '../api';

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
