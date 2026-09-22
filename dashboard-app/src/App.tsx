import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  AlertCircle, FileText, CheckCircle2, AlertTriangle, Briefcase, ChevronLeft,
  User, Landmark, History, Download, Network as NetIcon, Activity, Scale,
} from 'lucide-react';
import { ScrambleText } from './components/ScrambleText';
import { SpotlightCard } from './components/SpotlightCard';
import { Steering, DiffBanner } from './components/Steering';
import { Decide, RouteChip } from './components/Decide';
import { Waterfall, Network, Timeline, PriorCase } from './components/Views';
import { api, type Diff } from './api';

const SOURCE_ICON: Record<string, React.ReactNode> = {
  graph: <AlertCircle className="w-5 h-5 text-crimson" />,
  document: <FileText className="w-5 h-5 text-emerald-700" />,
  customer: <User className="w-5 h-5 text-amber-600" />,
  external: <Landmark className="w-5 h-5 text-muted" />,
};

export default function App() {
  const [data, setData] = useState<any[]>([]);
  const [selectedCase, setSelectedCase] = useState<string | null>(null);
  const [tab, setTab] = useState<'cases' | 'monitoring'>('cases');
  const [diff, setDiff] = useState<{ diff?: Diff; note?: string } | null>(null);
  const [events, setEvents] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);     // the open case, in full
  const [prior, setPrior] = useState<string | null>(null);
  const [busiest, setBusiest] = useState(true);        // queue order vs case order

  useEffect(() => {
    api.cases().then(setData).catch((e) =>
      setError(`Could not reach the console API. Start it with ` +
        `\`uv run uvicorn server:app --port 8000\`. (${e})`));
  }, []);

  // The desk works the money, not the case numbers: exposure x probability is the
  // order an analyst would pick up, and it is the order the queue defaults to.
  const filteredData = data.filter((d) => d.source === tab).slice().sort((a, b) =>
    busiest
      ? (b.case.exposure_usd * b.case.fraud_probability)
        - (a.case.exposure_usd * a.case.fraud_probability)
      : a.case_id.localeCompare(b.case_id));
  const activeCase = detail && detail.case_id === selectedCase
    ? detail : data.find((d) => d.case_id === selectedCase);

  const replaceCase = (updated: any, signals?: any[]) => {
    setData((prev) => prev.map((d) =>
      d.case_id === selectedCase ? { ...d, ...updated } : d));
    setDetail((prev: any) => prev ? { ...prev, ...updated,
      ...(signals ? { signals } : {}) } : prev);
  };

  const open = (id: string) => {
    setSelectedCase(id); setDiff(null); setEvents([]); setDetail(null);
    api.case(id).then(setDetail).catch(() => {});
  };

  // Analysts work queues with their hands on the keyboard: j/k to move, Enter to open,
  // Escape to come back, / to start arguing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(
        (e.target as HTMLElement)?.tagName ?? '');
      if (e.key === '/' && !typing) {
        e.preventDefault();
        document.querySelector<HTMLInputElement>('input[placeholder*="holiday"]')?.focus();
        return;
      }
      if (typing) return;
      if (e.key === 'Escape') return setSelectedCase(null);
      if (e.key !== 'j' && e.key !== 'k') return;
      const ids = filteredData.map((d) => d.case_id);
      const at = selectedCase ? ids.indexOf(selectedCase) : -1;
      const next = e.key === 'j' ? Math.min(at + 1, ids.length - 1) : Math.max(at - 1, 0);
      if (ids[next]) open(ids[next]);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [filteredData, selectedCase]);

  const verdictColor = (v: string) =>
    v === 'fraud' ? 'text-crimson' : v === 'legitimate' ? 'text-emerald-700' : 'text-amber-600';
  const verdictBg = (v: string) =>
    v === 'fraud' ? 'bg-crimson/10' : v === 'legitimate' ? 'bg-emerald-100' : 'bg-amber-100';

  return (
    <div className="flex h-screen overflow-hidden bg-paper font-sans">
      <motion.div
        initial={{ x: -50, opacity: 0 }} animate={{ x: 0, opacity: 1 }}
        transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
        className="w-[380px] border-r border-line bg-white flex flex-col z-10 shadow-sm shrink-0"
      >
        <div className="p-8 border-b border-line">
          <h1 className="text-2xl font-serif font-bold tracking-tight text-ink mb-1 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-crimson inline-block animate-pulse" />
            Investigation Desk
          </h1>
          <p className="text-muted text-sm font-mono tracking-tight uppercase">
            TigerGraph agentic fraud review
          </p>
          <div className="flex gap-2 mt-8 bg-paper p-1 rounded-sm">
            {(['cases', 'monitoring'] as const).map((t) => (
              <button key={t} onClick={() => { setTab(t); setSelectedCase(null); }}
                className={`flex-1 py-2 text-xs font-bold uppercase tracking-wider transition-colors
                  ${tab === t ? 'bg-white shadow-sm text-ink' : 'text-muted hover:text-ink'}`}>
                {t === 'cases' ? 'Cases' : 'Self-opened'}
              </button>
            ))}
          </div>
        </div>

        <button onClick={() => setBusiest((b) => !b)}
          className="px-8 py-3 text-left text-[10px] font-mono uppercase tracking-widest
                     text-muted hover:text-ink border-b border-line">
          {busiest ? 'Ordered by exposure x probability' : 'Ordered by case id'} · click to swap
        </button>

        <div className="flex-1 overflow-y-auto">
          {error && <div className="p-6 text-sm text-crimson leading-relaxed">{error}</div>}
          {filteredData.map((item, idx) => (
            <motion.div
              initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
              transition={{ delay: Math.min(idx * 0.04, 0.5) }}
              key={item.case_id} onClick={() => open(item.case_id)}
              className={`p-6 border-b border-line cursor-pointer transition-all
                ${selectedCase === item.case_id
                  ? 'bg-crimson/5 border-l-4 border-l-crimson'
                  : 'hover:bg-paper border-l-4 border-l-transparent'}`}
            >
              <div className="flex justify-between items-start mb-2">
                <span className="font-mono text-sm font-bold text-ink">{item.case_id}</span>
                <span className={`text-[10px] font-bold uppercase tracking-widest px-2 py-1
                  ${verdictBg(item.case.verdict)} ${verdictColor(item.case.verdict)}`}>
                  {item.closed ? 'closed' : item.case.verdict}
                </span>
              </div>
              <div className="flex justify-between items-end mt-4">
                <div className="text-2xl font-serif tracking-tight font-medium text-ink">
                  ${item.case.exposure_usd.toLocaleString('en-US', { minimumFractionDigits: 2 })}
                </div>
                <div className="text-xs text-muted font-mono">
                  p {item.case.fraud_probability.toFixed(2)}
                </div>
              </div>
            </motion.div>
          ))}
        </div>
      </motion.div>

      <div className="flex-1 overflow-y-auto relative bg-[#fafafa]">
        <div className="absolute inset-0 pointer-events-none opacity-5"
          style={{
            backgroundImage: 'linear-gradient(#000 1px, transparent 1px), linear-gradient(90deg, #000 1px, transparent 1px)',
            backgroundSize: '40px 40px',
          }} />

        <AnimatePresence mode="wait">
          {!activeCase ? <Brief key="brief" rows={filteredData} /> : (
            <motion.div key={activeCase.case_id}
              initial={{ opacity: 0, y: 30 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
              transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
              className="p-16 max-w-5xl mx-auto relative"
            >
              <button onClick={() => setSelectedCase(null)}
                className="flex items-center gap-2 text-sm font-bold uppercase tracking-widest
                           text-muted hover:text-ink transition-colors mb-12">
                <ChevronLeft className="w-4 h-4" /> Back to brief
              </button>

              <div className="flex items-center gap-4 mb-6">
                <span className={`text-xs font-bold uppercase tracking-widest px-3 py-1
                  ${verdictBg(activeCase.case.verdict)} ${verdictColor(activeCase.case.verdict)}`}>
                  {activeCase.case.verdict}
                </span>
                <span className="font-mono text-sm text-muted">{activeCase.case.status}</span>
                <span className="font-mono text-xs text-muted">{activeCase.case.pattern}</span>
              </div>

              <h2 className="text-7xl font-serif font-black tracking-tighter mb-3 text-ink leading-none">
                {activeCase.case_id}
              </h2>
              <p className="font-serif italic text-xl text-muted mb-10 max-w-3xl">
                {activeCase.trigger?.trigger_text ?? ''}
              </p>

              <Stats c={activeCase} />

              {activeCase.case.pattern_description && (
                <div className="border-l-4 border-amber-500 bg-amber-50 px-6 py-4 mb-10">
                  <div className="text-[10px] font-bold uppercase tracking-widest text-amber-700 mb-2">
                    Undocumented pattern — described by the agent
                  </div>
                  <p className="font-serif text-lg leading-relaxed text-ink/80">
                    {activeCase.case.pattern_description}
                  </p>
                </div>
              )}

              <AnimatePresence>
                {diff && <DiffBanner diff={diff.diff} note={diff.note} />}
              </AnimatePresence>

              <Steering caseId={activeCase.case_id}
                steered={Boolean(activeCase.suppressed?.length || activeCase.ring_cap)}
                onResult={(r: any) => {
                  if (r.case) replaceCase(r.case, r.signals);
                  setDiff({ diff: r.changed, note: r.note });
                  api.case(activeCase.case_id).then(setDetail).catch(() => {});
                }} />

              {activeCase.signals && (
                <Section title="How the probability was reached"
                  icon={<Scale className="w-7 h-7 text-muted" />}>
                  <Waterfall signals={activeCase.signals}
                    final={activeCase.case.fraud_probability} />
                </Section>
              )}

              <Section title="The graph the investigation walked"
                icon={<NetIcon className="w-7 h-7 text-muted" />}>
                <Network caseId={activeCase.case_id} onPickCase={setPrior} />
              </Section>

              <Section title="The episode" icon={<Activity className="w-7 h-7 text-muted" />}>
                <Timeline caseId={activeCase.case_id} />
              </Section>

              <Section title="Case summary" icon={<Briefcase className="w-7 h-7 text-muted" />}>
                <p className="text-xl leading-relaxed text-ink/80 font-serif border-l-4
                              border-crimson pl-6 bg-crimson/5 py-4">
                  {activeCase.case.summary}
                </p>
              </Section>

              <Section title={`Evidence (${activeCase.case.evidence.length})`}>
                <div className="space-y-4">
                  {activeCase.case.evidence.map((ev: any, i: number) => (
                    <SpotlightCard key={i} className={`p-6 flex gap-6 items-start
                      ${ev.claim.startsWith('WITHDRAWN') ? 'opacity-50' : ''}`}>
                      <div className="mt-1">{SOURCE_ICON[ev.source] ?? SOURCE_ICON.external}</div>
                      <div>
                        <div className="text-xs font-bold uppercase tracking-widest text-muted mb-2 font-mono">
                          {ev.source} &mdash; {ev.ref}
                        </div>
                        <div className="text-lg text-ink font-medium leading-relaxed">{ev.claim}</div>
                      </div>
                    </SpotlightCard>
                  ))}
                </div>
              </Section>

              {activeCase.evidence_requests?.length > 0 && (
                <Section title="Evidence requested">
                  {activeCase.evidence_requests.map((q: any, i: number) => (
                    <div key={i} className="border-l-2 border-line pl-6 py-2 mb-4">
                      <div className="font-mono text-xs uppercase tracking-widest text-muted mb-2">
                        {q.type} · after step {q.asked_after_step}
                      </div>
                      <div className="text-ink/80 leading-relaxed">{q.assumed_response}</div>
                    </div>
                  ))}
                </Section>
              )}

              <Section title="Next best action">
                <div className="grid md:grid-cols-2 gap-8">
                  {(['initial', 'final'] as const).map((phase) => (
                    <div key={phase}>
                      <div className="font-mono text-[10px] uppercase tracking-widest text-muted mb-4">
                        {phase === 'initial' ? 'Before evidence' : 'After evidence'}
                      </div>
                      {activeCase.next_best_actions[phase].map((a: any, i: number) => (
                        <div key={i} className="border-b border-dashed border-line py-3">
                          <div className="flex items-center gap-3 mb-1">
                            <span className="font-mono text-sm font-bold text-ink">{a.action}</span>
                            <RouteChip route={a.route} />
                          </div>
                          <div className="text-sm text-muted leading-relaxed">{a.reason}</div>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
                <div className="mt-6 bg-paper border border-line p-4 text-sm text-ink/80 leading-relaxed">
                  <b className="font-bold">What changed:</b> {activeCase.next_best_actions.what_changed}
                </div>
              </Section>

              <Section title="Suspicious activity report">
                <div className="bg-ink text-paper p-10 shadow-2xl">
                  {activeCase.sar.file ? (
                    <>
                      <div className="text-crimson text-sm font-bold tracking-widest uppercase mb-6 flex items-center gap-2">
                        <CheckCircle2 className="w-5 h-5" /> Filed
                      </div>
                      <p className="font-serif text-lg leading-relaxed opacity-90 whitespace-pre-wrap">
                        {activeCase.sar.narrative}
                      </p>
                    </>
                  ) : (
                    <div className="text-paper/60 italic font-serif text-xl">
                      Not filed. {activeCase.sar.reason}
                    </div>
                  )}
                </div>
              </Section>

              <Section title="Why the investigation stopped">
                <p className="text-ink/80 leading-relaxed mb-6">{activeCase.stop_reason}</p>
                {activeCase.case.similar_prior_cases.length > 0 && (
                  <>
                    <div className="font-mono text-[10px] uppercase tracking-widest text-muted mb-3">
                      Prior cases retrieved as memory
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {activeCase.case.similar_prior_cases.map((id: string) => (
                        <button key={id} onClick={() => setPrior(id)}
                          className="font-mono text-xs bg-paper border border-line px-2 py-1
                                     hover:border-ink hover:text-ink text-muted transition-colors">
                          {id}
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </Section>

              <div className="flex flex-wrap items-center gap-4 mb-6 font-mono text-xs text-muted">
                <span className={activeCase.case.written_to_graph ? 'text-emerald-700' : ''}>
                  {activeCase.case.written_to_graph
                    ? `written to the graph as ${activeCase.case.graph_case_id}`
                    : 'not written to the graph (duckdb mirror)'}
                </span>
                <button onClick={() => download(activeCase)}
                  className="ml-auto flex items-center gap-2 border border-line px-3 py-2
                             uppercase tracking-widest hover:text-ink hover:border-ink transition-colors">
                  <Download className="w-4 h-4" /> Download case file
                </button>
              </div>

              <Decide caseId={activeCase.case_id} closed={activeCase.closed ?? null}
                device={activeCase.case.connected_device_profiles?.[0]}
                onEvent={(note, closed) => {
                  setEvents((e) => [note, ...e]);
                  if (closed) replaceCase({ closed });
                }} />

              {events.length > 0 && (
                <Section title="Audit trail" icon={<History className="w-7 h-7 text-muted" />}>
                  {events.map((e, i) => (
                    <div key={i} className="font-mono text-sm text-ink/80 border-l-2 border-crimson pl-4 py-2 mb-2">
                      {e}
                    </div>
                  ))}
                </Section>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {prior && <PriorCase id={prior} onClose={() => setPrior(null)} />}
    </div>
  );
}

/** The exact JSON that goes in the submission, minus the console's own bookkeeping. */
function download(c: any) {
  const { source, closed, trigger, events, signals, suppressed, ring_cap, ...answer } = c;
  const blob = new Blob([JSON.stringify(answer, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${c.case_id}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function Stats({ c }: { c: any }) {
  const cells: [string, string][] = [
    ['Exposure', `$${c.case.exposure_usd.toLocaleString()}`],
    ['Probability', c.case.fraud_probability.toFixed(2)],
    ['Affected txns', String(c.case.affected_txn_ids.length)],
    ['Connected cards', String(c.case.connected_card_ids.length)],
    ['Prior cases', String(c.case.similar_prior_cases.length)],
    ['Graph calls', String(c.tool_calls)],
  ];
  return (
    <div className="grid grid-cols-3 md:grid-cols-6 gap-px bg-line border border-line mb-10">
      {cells.map(([k, v]) => (
        <div key={k} className="bg-white p-5">
          <div className="text-[10px] font-bold text-muted uppercase tracking-widest mb-2">{k}</div>
          <div className="text-xl font-serif text-ink tabular-nums">{v}</div>
        </div>
      ))}
    </div>
  );
}

function Section({ title, icon, children }: {
  title: string; icon?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="mb-14">
      <h3 className="font-serif text-2xl font-bold mb-6 border-b-2 border-ink pb-3 flex items-center gap-3">
        {icon} {title}
      </h3>
      {children}
    </div>
  );
}

function Brief({ rows }: { rows: any[] }) {
  const exposure = rows.filter((d) => d.case.verdict === 'fraud')
    .reduce((a, b) => a + b.case.exposure_usd, 0);
  const patterns = Array.from(new Set(
    rows.filter((d) => d.case.verdict === 'fraud').map((d) => d.case.pattern))).slice(0, 3);
  return (
    <motion.div key="summary"
      initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, scale: 0.98 }}
      transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }} className="p-16 max-w-5xl mx-auto">
      <h2 className="text-6xl font-serif font-black tracking-tighter mb-4 text-ink leading-none">
        <ScrambleText text="The Daily Fraud Brief" />
      </h2>
      <p className="text-xl text-muted font-serif italic mb-16 max-w-2xl leading-relaxed">
        Every case below was investigated against the transaction graph and the bank's closed-case
        history. Open one to argue with the agent.
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-8 mb-16">
        <SpotlightCard className="p-10 bg-white">
          <h3 className="text-sm font-bold uppercase tracking-widest text-muted mb-6">Investigations</h3>
          <div className="text-6xl font-serif text-ink tracking-tighter font-medium">{rows.length}</div>
        </SpotlightCard>
        <SpotlightCard className="p-10 bg-white" spotlightColor="rgba(200, 16, 46, 0.15)">
          <h3 className="text-sm font-bold uppercase tracking-widest text-crimson mb-6">Assessed fraud exposure</h3>
          <div className="text-6xl font-serif text-crimson tracking-tighter font-medium">
            ${exposure.toLocaleString(undefined, { maximumFractionDigits: 0 })}
          </div>
        </SpotlightCard>
      </div>
      <h3 className="text-2xl font-serif font-bold text-ink border-b-2 border-ink pb-4 mb-8">
        Patterns identified
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {patterns.map((p) => (
          <div key={p} className="border border-line p-6 bg-white shadow-sm">
            <AlertTriangle className="w-6 h-6 text-crimson mb-4" />
            <div className="font-mono text-xs text-muted uppercase tracking-widest mb-2">Pattern</div>
            <div className="font-serif text-lg font-semibold leading-tight text-ink">
              {String(p).replace(/_/g, ' ')}
            </div>
          </div>
        ))}
      </div>
    </motion.div>
  );
}
