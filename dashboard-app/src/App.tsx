import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { AlertCircle, FileText, CheckCircle2, AlertTriangle, ShieldX, Briefcase, ChevronLeft, ShieldAlert, Cpu, Send, Check, X, ServerCrash, Unlock, Scale, Activity, Download, Share2 } from 'lucide-react';
import { ScrambleText } from './components/ScrambleText';
import { SpotlightCard } from './components/SpotlightCard';
import { Waterfall, Network, Timeline, PriorCase } from './components/Views';
import { RouteChip, OpenCase } from './components/Decide';
import { ACTIONS, api, authHeaders, token } from './api';
import { Intelligence, Ledger } from './components/Intel';

const API = '/api';   // through the Vite proxy: same origin, no CORS

export default function App() {
  const [data, setData] = useState<any[]>([]);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [activeCase, setActiveCase] = useState<any | null>(null);
  const [tab, setTab] = useState<'cases' | 'monitoring' | 'api' | 'intel'>('cases');
  const [ledgerTick, setLedgerTick] = useState(0);
  
  // Interactions
  const [challengeText, setChallengeText] = useState('');
  const [overrideAction, setOverrideAction] = useState('');
  const [prior, setPrior] = useState<string | null>(null);
  const [byMoney, setByMoney] = useState(true);
  const [loading, setLoading] = useState(false);
  const [serverError, setServerError] = useState(false);
  const [notice, setNotice] = useState('');
  const [tok, setTok] = useState(token.get());

  useEffect(() => {
    fetchCases();
  }, []);

  const fetchCases = () => {
    fetch(`${API}/cases`)
      .then((res) => res.json())
      .then((d) => {
        setData(d);
        setServerError(false);
      })
      .catch((e) => {
        console.error("Backend unreachable", e);
        setServerError(true);
      });
  };

  useEffect(() => {
    if (selectedCaseId) {
      setLoading(true);
      fetch(`${API}/case/${selectedCaseId}`)
        .then((res) => res.json())
        .then((d) => { setActiveCase(d); setLoading(false); })
        .catch((e) => { console.error(e); setLoading(false); });
    } else {
      setActiveCase(null);
    }
  }, [selectedCaseId]);

  const runAction = async (endpoint: string, payload?: any) => {
    if (!selectedCaseId) return;
    setLoading(true);
    try {
      const res = await fetch(`${API}/case/${selectedCaseId}/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: payload ? JSON.stringify(payload) : undefined
      });
      // a refused change must say so; it used to fail silently
      setNotice(res.ok ? '' : `${endpoint}: ${(await res.text()).slice(0, 200)}`);
      // Refresh active case state
      const r = await fetch(`${API}/case/${selectedCaseId}`);
      setActiveCase(await r.json());
      fetchCases(); // Refresh sidebar global state
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
    setChallengeText('');
    setLedgerTick(t => t + 1);
  };

  // The desk works the money, not the case numbers: exposure x probability is the order
  // an analyst picks cases up in, and it is what the queue defaults to.
  const filteredData = data.filter(d => d.source === tab).slice().sort((a, b) =>
    byMoney
      ? (b.case.exposure_usd * b.case.fraud_probability)
        - (a.case.exposure_usd * a.case.fraud_probability)
      : a.case_id.localeCompare(b.case_id));

  // Analysts work queues with their hands on the keyboard.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(
        (e.target as HTMLElement)?.tagName ?? '');
      if (e.key === '/' && !typing) {
        e.preventDefault();
        document.querySelector<HTMLInputElement>('input[placeholder^="e.g."]')?.focus();
        return;
      }
      if (typing) return;
      if (e.key === 'Escape') { setSelectedCaseId(null); return; }
      if (e.key !== 'j' && e.key !== 'k') return;
      const ids = filteredData.map(d => d.case_id);
      const at = selectedCaseId ? ids.indexOf(selectedCaseId) : -1;
      const next = e.key === 'j' ? Math.min(at + 1, ids.length - 1) : Math.max(at - 1, 0);
      if (ids[next]) setSelectedCaseId(ids[next]);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [filteredData, selectedCaseId]);

  const formatSignalName = (name: string) => {
    if (!name) return '';
    if (name.startsWith('DOC:')) {
      const parts = name.replace('DOC:', '').split('::');
      return `${parts[0].replace(/_/g, ' ').trim()} — ${parts[1]?.replace(/_/g, ' ').trim() || ''}`;
    }
    
    const formattingMap: Record<string, string> = {
      'm_flags': 'Card Match Flags (M1-M9)',
      'prior_fraud': 'Prior Fraud History',
      'prior_cleared': 'Prior Cleared History',
      'step_up': 'Step-Up Authentication',
      'risk_score': 'Bank Risk Score',
      'risk_score_high': 'High Risk Score',
      'risk_score_mid': 'Medium Risk Score',
      'device_new': 'New Device Profile',
      'device_ring': 'Device Ring Detected',
      'ring_component': 'Connected Graph Component',
      'amount_outlier': 'Transaction Amount Outlier',
      'channel_odd': 'Anomalous Channel',
      'recurring': 'Recurring Subscription',
      'proxy': 'VPN / Proxy Use',
      'proxy_noted': 'VPN / Proxy Noted',
      'proxy_new_device': 'VPN on New Device',
      'region_new_bad': 'Out of Region (High Risk)',
      'region_new_travel': 'Out of Region (Travel)',
      'analyst_context': 'Analyst Override Context'
    };

    const cleanName = name.replace('withdrawn:', '').trim().toLowerCase();
    const prefix = name.startsWith('withdrawn:') ? '[WITHDRAWN] ' : '';
    
    if (formattingMap[cleanName]) {
      return prefix + formattingMap[cleanName];
    }
    return prefix + cleanName.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  };

  const getVerdictColor = (verdict: string) => {
    if (verdict === 'fraud') return 'text-crimson';
    if (verdict === 'legitimate') return 'text-emerald-700';
    return 'text-amber-600';
  };

  const getVerdictBg = (verdict: string) => {
    if (verdict === 'fraud') return 'bg-crimson/10';
    if (verdict === 'legitimate') return 'bg-emerald-100';
    return 'bg-amber-100';
  };

  if (serverError) {
    return (
      <div className="flex h-screen items-center justify-center bg-paper">
        <div className="text-center">
          <ServerCrash className="w-16 h-16 text-muted mx-auto mb-6" />
          <h1 className="text-2xl font-serif text-ink mb-2">Backend Disconnected</h1>
          <p className="text-muted font-mono text-sm">Please start the FastAPI server: <br/> <code className="bg-line px-2 py-1 mt-2 inline-block">uv run uvicorn server:app --port 8000</code></p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden bg-paper font-sans">
      {notice && (
        <div role="alert" onClick={() => setNotice('')}
          className="fixed bottom-4 right-4 z-50 max-w-md bg-crimson text-white text-sm px-4 py-3 shadow-editorial cursor-pointer">
          {notice}
        </div>
      )}
      
      {/* Sidebar */}
      <motion.div 
        initial={{ x: -50, opacity: 0 }}
        animate={{ x: 0, opacity: 1 }}
        transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
        className="w-[380px] border-r border-line bg-white flex flex-col z-10 shadow-sm shrink-0"
      >
        <div className="p-8 border-b border-line">
          <h1 className="text-2xl font-serif font-bold tracking-tight text-ink mb-1 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-crimson inline-block animate-pulse"></span>
            Investigation Desk
          </h1>
          <p className="text-muted text-sm font-mono tracking-tight uppercase">TigerGraph Command Center</p>
          
          <div className="flex gap-2 mt-8 bg-paper p-1 rounded-sm">
            <button 
              onClick={() => {setTab('cases'); setSelectedCaseId(null);}} 
              className={`flex-1 py-2 text-xs font-bold uppercase tracking-wider transition-colors ${tab === 'cases' ? 'bg-white shadow-sm text-ink' : 'text-muted hover:text-ink'}`}
            >
              Cases
            </button>
            <button 
              onClick={() => {setTab('monitoring'); setSelectedCaseId(null);}} 
              className={`flex-1 py-2 text-xs font-bold uppercase tracking-wider transition-colors ${tab === 'monitoring' ? 'bg-white shadow-sm text-ink' : 'text-muted hover:text-ink'}`}
            >
              Monitoring
            </button>
            <button
              onClick={() => {setTab('api'); setSelectedCaseId(null);}}
              className={`flex-1 py-2 text-xs font-bold uppercase tracking-wider transition-colors ${tab === 'api' ? 'bg-white shadow-sm text-ink' : 'text-muted hover:text-ink'}`}
            >
              Live
            </button>
            <button
              onClick={() => {setTab('intel'); setSelectedCaseId(null);}}
              className={`flex-1 py-2 text-xs font-bold uppercase tracking-wider transition-colors ${tab === 'intel' ? 'bg-white shadow-sm text-ink' : 'text-muted hover:text-ink'}`}
            >
              Recurring
            </button>
          </div>
          <input value={tok} type="password" autoComplete="off" aria-label="Analyst token"
            onChange={(e) => { setTok(e.target.value); token.set(e.target.value); }}
            placeholder="Analyst token (needed for any change)"
            className="mt-4 w-full border border-line px-3 py-2 text-xs font-mono bg-paper focus:outline-none focus:border-crimson" />
        </div>

        <button onClick={() => setByMoney(b => !b)}
          className="px-8 py-3 text-left text-[10px] font-mono uppercase tracking-widest
                     text-muted hover:text-ink border-b border-line">
          {byMoney ? 'Ordered by exposure x probability' : 'Ordered by case id'} · click to swap
        </button>

        <div className="flex-1 overflow-y-auto">
          {tab === 'api' && (
            <OpenCase onOpened={(id) => { fetchCases(); setSelectedCaseId(id); }} />
          )}
          {filteredData.map((item, idx) => (
            <motion.div 
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: idx * 0.05 }}
              key={item.case_id}
              onClick={() => setSelectedCaseId(item.case_id)}
              className={`p-6 border-b border-line cursor-pointer transition-all ${selectedCaseId === item.case_id ? 'bg-crimson/5 border-l-4 border-l-crimson' : 'hover:bg-paper border-l-4 border-l-transparent'}`}
            >
              <div className="flex justify-between items-start mb-2">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm font-bold text-ink">{item.case_id}</span>
                  {item.closed && <span className="bg-ink text-white text-[9px] px-1.5 uppercase font-bold tracking-widest rounded-sm">Closed</span>}
                </div>
                <span className={`text-[10px] font-bold uppercase tracking-widest px-2 py-1 ${getVerdictBg(item.case.verdict)} ${getVerdictColor(item.case.verdict)}`}>
                  {item.case.verdict}
                </span>
              </div>
              <div className="flex justify-between items-end mt-4">
                <div className="text-xl font-serif tracking-tight font-medium text-ink">
                  ${item.case.exposure_usd.toLocaleString('en-US', {minimumFractionDigits:2})}
                </div>
                <div className="text-xs text-muted font-mono">Prob: {item.case.fraud_probability.toFixed(2)}</div>
              </div>
            </motion.div>
          ))}
        </div>
      </motion.div>

      {/* Main Content Area */}
      <div className="flex-1 overflow-y-auto relative bg-[#fafafa]">
        <div className="absolute inset-0 pointer-events-none opacity-5" 
          style={{ backgroundImage: 'linear-gradient(#000 1px, transparent 1px), linear-gradient(90deg, #000 1px, transparent 1px)', backgroundSize: '40px 40px' }}>
        </div>

        <AnimatePresence mode="wait">
          {tab === 'intel' ? (
            <Intelligence key="intel" onPickCase={(id) => {
              // A closed case opens in the drill-down; one of ours opens as a case.
              const mine = data.find(d => d.case?.graph_case_id === id || d.case_id === id);
              if (mine) { setTab(mine.source); setSelectedCaseId(mine.case_id); }
              else setPrior(id);
            }} />
          ) : !activeCase ? (
            <motion.div 
              key="summary"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.98 }}
              transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
              className="p-16 max-w-5xl mx-auto"
            >
              <h2 className="text-6xl font-serif font-black tracking-tighter mb-4 text-ink leading-none">
                <ScrambleText text="The Daily Fraud Brief" />
              </h2>
              <p className="text-xl text-muted font-serif italic mb-16 max-w-2xl leading-relaxed">
                An executive summary of today's agentic investigations across all audited financial streams, running live against TigerGraph.
              </p>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-8 mb-16">
                <SpotlightCard className="p-10 bg-white">
                  <h3 className="text-sm font-bold uppercase tracking-widest text-muted mb-6">Total Evaluated Volume</h3>
                  <div className="text-6xl font-serif text-ink tracking-tighter font-medium">{filteredData.length} <span className="text-2xl text-muted font-sans font-normal tracking-normal">Investigations</span></div>
                </SpotlightCard>
                <SpotlightCard className="p-10 bg-white" spotlightColor="rgba(200, 16, 46, 0.15)">
                  <h3 className="text-sm font-bold uppercase tracking-widest text-crimson mb-6">Confirmed Fraud Exposure</h3>
                  <div className="text-6xl font-serif text-crimson tracking-tighter font-medium">
                    ${filteredData.filter(d => d.case.verdict === 'fraud').reduce((a, b) => a + b.case.exposure_usd, 0).toLocaleString()}
                  </div>
                </SpotlightCard>
              </div>

              <h3 className="text-2xl font-serif font-bold text-ink border-b-2 border-ink pb-4 mb-8">Notable Patterns Identified</h3>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                {Array.from(new Set(filteredData.filter(d => d.case.verdict === 'fraud').map(d => d.case.pattern))).slice(0,3).map(pattern => (
                  <div key={pattern} className="border border-line p-6 bg-white shadow-sm">
                    <AlertTriangle className="w-6 h-6 text-crimson mb-4" />
                    <div className="font-mono text-xs text-muted uppercase tracking-widest mb-2">Pattern</div>
                    <div className="font-serif text-lg font-semibold leading-tight text-ink">{pattern ? pattern.replace(/_/g, ' ') : 'Multiple Anomalies'}</div>
                  </div>
                ))}
              </div>
            </motion.div>
          ) : (
            <motion.div 
              key="detail"
              initial={{ opacity: 0, y: 30 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
              className={`p-16 max-w-5xl mx-auto ${loading ? 'opacity-50 pointer-events-none' : ''}`}
            >
              <button 
                onClick={() => setSelectedCaseId(null)}
                className="flex items-center gap-2 text-sm font-bold uppercase tracking-widest text-muted hover:text-ink transition-colors mb-12"
              >
                <ChevronLeft className="w-4 h-4" /> Back to Brief
              </button>

              <div className="flex items-center gap-4 mb-6">
                <span className={`text-xs font-bold uppercase tracking-widest px-3 py-1 ${getVerdictBg(activeCase.case.verdict)} ${getVerdictColor(activeCase.case.verdict)}`}>
                  {activeCase.case.verdict}
                </span>
                <span className="font-mono text-sm text-muted">{activeCase.case.status}</span>
                {activeCase.closed && (
                  <span className="font-mono text-sm font-bold px-2 py-1 bg-ink text-white">LOCKED: {activeCase.closed}</span>
                )}
              </div>

              <h2 className="text-7xl font-serif font-black tracking-tighter mb-8 text-ink leading-none">
                {activeCase.case_id}
              </h2>

              <div className="grid grid-cols-4 gap-px bg-line border border-line mb-12 shadow-sm">
                <div className="bg-white p-6">
                  <div className="text-[10px] font-bold text-muted uppercase tracking-widest mb-2">Exposure</div>
                  <div className="text-2xl font-serif text-ink">${activeCase.case.exposure_usd.toLocaleString()}</div>
                </div>
                <div className="bg-white p-6 relative">
                  <div className="text-[10px] font-bold text-muted uppercase tracking-widest mb-2">Risk Score</div>
                  <div className="text-2xl font-serif text-ink">{activeCase.case.fraud_probability.toFixed(2)}</div>
                </div>
                <div className="bg-white p-6">
                  <div className="text-[10px] font-bold text-muted uppercase tracking-widest mb-2">Affected Txns</div>
                  <div className="text-2xl font-serif text-ink">{activeCase.case.affected_txn_ids.length}</div>
                </div>
                <div className="bg-white p-6">
                  <div className="text-[10px] font-bold text-muted uppercase tracking-widest mb-2">Suppressed</div>
                  <div className="text-2xl font-serif text-ink">{activeCase.suppressed?.length || 0}</div>
                </div>
              </div>

              {/* INTERACTIVE CONTROL PANEL */}
              {!activeCase.closed && (
                <div className="mb-16 border border-ink p-8 bg-white shadow-editorial">
                  <h3 className="font-serif text-xl font-bold mb-6 flex items-center gap-2 border-b border-line pb-4">
                    <Cpu className="w-5 h-5" /> Agentic Override Controls
                  </h3>
                  
                  {/* Steering */}
                  <div className="mb-8">
                    <label className="block text-xs font-bold uppercase tracking-widest text-muted mb-3">Challenge AI Reasoning</label>
                    <div className="flex gap-2">
                      <input 
                        type="text" 
                        value={challengeText}
                        onChange={(e) => setChallengeText(e.target.value)}
                        placeholder="e.g. Ignore the out-of-region flag, customer is on holiday"
                        className="flex-1 bg-paper border border-line p-3 text-sm focus:outline-none focus:border-ink font-mono"
                      />
                      <button 
                        onClick={() => runAction('challenge', { text: challengeText })}
                        disabled={challengeText.length < 3}
                        className="bg-ink text-white px-6 py-3 text-sm font-bold uppercase tracking-widest hover:bg-ink/80 transition-colors disabled:opacity-50"
                      >
                        <Send className="w-4 h-4" />
                      </button>
                    </div>
                    {activeCase.suppressed?.length > 0 && (
                      <div className="mt-3 text-xs text-muted font-mono flex items-center gap-4">
                        <span>Suppressed signals: {activeCase.suppressed.join(', ')}</span>
                        <button onClick={() => runAction('reset')} className="text-crimson underline">Reset Agent State</button>
                      </div>
                    )}
                  </div>

                  <div className="flex gap-4 mb-8">
                    <button onClick={() => runAction('stepup')} className="flex-1 bg-paper border border-line py-3 text-xs font-bold uppercase tracking-widest hover:bg-line transition-colors flex items-center justify-center gap-2">
                      <Unlock className="w-4 h-4" /> Request Step-Up Auth
                    </button>
                    <button onClick={() => runAction('deepen', { cap: 20 })} className="flex-1 bg-paper border border-line py-3 text-xs font-bold uppercase tracking-widest hover:bg-line transition-colors flex items-center justify-center gap-2">
                      <ShieldAlert className="w-4 h-4" /> Deepen Graph Expansion
                    </button>
                  </div>

                  {/* Overriding does not bypass the policy: route_for still decides the
                      approval route, so BLOCK_ALL_CARDS still comes back L2. */}
                  <div className="flex gap-2 mb-8">
                    <select value={overrideAction} onChange={(e) => setOverrideAction(e.target.value)}
                      className="flex-1 bg-paper border border-line p-3 text-xs font-mono focus:outline-none focus:border-ink">
                      <option value="">Override with a different action…</option>
                      {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                    </select>
                    <button
                      onClick={() => { runAction('decision', { decision: 'override', action: overrideAction }); setOverrideAction(''); }}
                      disabled={!overrideAction}
                      className="border border-ink px-6 text-xs font-bold uppercase tracking-widest
                                 hover:bg-ink hover:text-white transition-colors disabled:opacity-30">
                      Override
                    </button>
                  </div>

                  <div className="flex gap-4 pt-6 border-t border-line">
                    <button onClick={() => runAction('decision', { decision: 'approve' })} className="flex-1 bg-emerald-700 text-white py-3 text-xs font-bold uppercase tracking-widest hover:bg-emerald-800 transition-colors flex items-center justify-center gap-2">
                      <Check className="w-4 h-4" /> Approve & Execute
                    </button>
                    <button onClick={() => runAction('close', { outcome: 'confirmed_fraud' })} className="flex-1 bg-crimson text-white py-3 text-xs font-bold uppercase tracking-widest hover:bg-crimson/90 transition-colors flex items-center justify-center gap-2">
                      <ShieldX className="w-4 h-4" /> Close Case: Fraud
                    </button>
                    <button onClick={() => runAction('close', { outcome: 'cleared' })} className="flex-1 bg-ink text-white py-3 text-xs font-bold uppercase tracking-widest hover:bg-ink/90 transition-colors flex items-center justify-center gap-2">
                      <X className="w-4 h-4" /> Close Case: False Positive
                    </button>
                  </div>
                </div>
              )}

              {activeCase.case.pattern_description && (
                <div className="border-l-4 border-amber-500 bg-amber-50 px-6 py-4 mb-12">
                  <div className="text-[10px] font-bold uppercase tracking-widest text-amber-700 mb-2">
                    Undocumented pattern — described by the agent
                  </div>
                  <p className="font-serif text-lg leading-relaxed text-ink/80">
                    {activeCase.case.pattern_description}
                  </p>
                </div>
              )}

              <div className="prose prose-lg max-w-none">
                {activeCase.signals?.length > 0 && (
                  <>
                    <h3 className="font-serif text-3xl font-bold mb-6 flex items-center gap-3">
                      <Scale className="w-8 h-8 text-muted" /> How the probability was reached
                    </h3>
                    <div className="mb-16">
                      <Waterfall signals={activeCase.signals}
                        final={activeCase.case.fraud_probability} />
                    </div>
                  </>
                )}

                <h3 className="font-serif text-3xl font-bold mb-6 flex items-center gap-3">
                  <Share2 className="w-8 h-8 text-muted" /> The graph the investigation walked
                </h3>
                <div className="mb-16">
                  <Network caseId={activeCase.case_id} onPickCase={setPrior} />
                </div>

                <h3 className="font-serif text-3xl font-bold mb-6 flex items-center gap-3">
                  <Activity className="w-8 h-8 text-muted" /> The episode
                </h3>
                <div className="mb-16"><Timeline caseId={activeCase.case_id} /></div>

                <h3 className="font-serif text-3xl font-bold mb-6 flex items-center gap-3">
                  <Briefcase className="w-8 h-8 text-muted" /> Executive Summary
                </h3>
                <p className="text-xl leading-relaxed text-ink/80 font-serif mb-12 border-l-4 border-crimson pl-6 bg-crimson/5 py-4">
                  {activeCase.case.summary}
                </p>

                {/* Evidence Decomposition */}
                <h3 className="font-serif text-3xl font-bold mb-8 border-b-2 border-ink pb-4">Decomposed Evidence Array</h3>
                <div className="space-y-4 mb-16">
                  {activeCase.signals?.map((sig: any, i: number) => (
                    <SpotlightCard key={i} className={`p-6 flex gap-6 items-start ${sig.withdrawn ? 'opacity-50 bg-paper' : ''}`}>
                      <div className="mt-1">
                        {sig.source === 'graph' ? <AlertCircle className="w-5 h-5 text-crimson" /> : <FileText className="w-5 h-5 text-emerald-700" />}
                      </div>
                      <div className="flex-1">
                        <div className="flex justify-between mb-2">
                          <span className="text-xs font-bold uppercase tracking-widest text-muted font-mono">{formatSignalName(sig.name)}</span>
                          <span className="text-xs font-mono font-bold">{sig.weight > 0 ? '+' : ''}{sig.weight}</span>
                        </div>
                        <div className={`text-lg text-ink font-medium leading-relaxed ${sig.withdrawn ? 'line-through' : ''}`}>{sig.claim}</div>
                      </div>
                    </SpotlightCard>
                  ))}
                </div>

                {activeCase.evidence_requests?.length > 0 && (
                  <>
                    <h3 className="font-serif text-3xl font-bold mb-8 border-b-2 border-ink pb-4">Evidence Requested</h3>
                    <div className="mb-16">
                      {activeCase.evidence_requests.map((q: any, i: number) => (
                        <div key={i} className="border-l-2 border-line pl-6 py-2 mb-4">
                          <div className="font-mono text-xs uppercase tracking-widest text-muted mb-2">
                            {q.type} · after step {q.asked_after_step}
                          </div>
                          <div className="text-ink/80 leading-relaxed">{q.assumed_response}</div>
                          {q.simulated && !activeCase.closed && (
                            <div className="flex flex-wrap gap-2 mt-3 items-center">
                              <span className="font-mono text-[10px] uppercase tracking-widest text-muted">Record the real reply:</span>
                              {(['confirmed', 'denied', 'no_reply'] as const).map((o) => (
                                <button key={o} disabled={loading}
                                  onClick={async () => {
                                    setLoading(true);
                                    try {
                                      await api.reply(activeCase.case_id, q.type, o, '');
                                      const r = await fetch(`${API}/case/${activeCase.case_id}`);
                                      setActiveCase(await r.json());
                                      fetchCases(); setNotice('');
                                    } catch (e) { setNotice(String(e).slice(0, 200)); }
                                    setLoading(false);
                                  }}
                                  className="px-3 py-1 border border-line text-[10px] font-bold uppercase tracking-widest hover:border-ink hover:text-ink transition-colors disabled:opacity-30">
                                  {o.replace('_', ' ')}
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </>
                )}

                {/* Part 3 of the answer spec: what to do, before and after the evidence
                    came back, each with the approval route the policy assigns it. */}
                <h3 className="font-serif text-3xl font-bold mb-8 border-b-2 border-ink pb-4">Next Best Action</h3>
                <div className="grid md:grid-cols-2 gap-10 mb-8">
                  {(['initial', 'final'] as const).map((phase) => (
                    <div key={phase}>
                      <div className="font-mono text-[10px] uppercase tracking-widest text-muted mb-4">
                        {phase === 'initial' ? 'Before evidence' : 'After evidence'}
                      </div>
                      {activeCase.next_best_actions?.[phase]?.map((a: any, i: number) => (
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
                <div className="bg-paper border border-line p-4 text-sm text-ink/80 leading-relaxed mb-16">
                  <b className="font-bold">What changed:</b> {activeCase.next_best_actions?.what_changed}
                </div>

                <h3 className="font-serif text-3xl font-bold mb-8 border-b-2 border-ink pb-4">Regulatory Action (SAR)</h3>
                <div className="bg-ink text-paper p-10 rounded-sm mb-16 shadow-2xl">
                  {activeCase.sar?.file ? (
                    <>
                      <div className="text-crimson text-sm font-bold tracking-widest uppercase mb-6 flex items-center gap-2">
                        <CheckCircle2 className="w-5 h-5" /> Pending SAR
                      </div>
                      <p className="font-serif text-lg leading-relaxed opacity-90 whitespace-pre-wrap">
                        {activeCase.sar.narrative}
                      </p>
                    </>
                  ) : (
                    <div className="text-paper/60 italic font-serif text-xl">Not filed. {activeCase.sar?.reason}</div>
                  )}
                </div>

                <h3 className="font-serif text-3xl font-bold mb-8 border-b-2 border-ink pb-4">Why the Investigation Stopped</h3>
                <p className="text-ink/80 leading-relaxed mb-8">{activeCase.stop_reason}</p>
                {activeCase.case.similar_prior_cases?.length > 0 && (
                  <>
                    <div className="font-mono text-[10px] uppercase tracking-widest text-muted mb-3">
                      Prior cases retrieved as memory — click one to read it
                    </div>
                    <div className="flex flex-wrap gap-2 mb-8">
                      {activeCase.case.similar_prior_cases.map((id: string) => (
                        <button key={id} onClick={() => setPrior(id)}
                          className="font-mono text-xs bg-paper border border-line px-2 py-1
                                     text-muted hover:border-ink hover:text-ink transition-colors">
                          {id}
                        </button>
                      ))}
                    </div>
                  </>
                )}
                <div className="flex flex-wrap items-center gap-4 mb-16 font-mono text-xs text-muted">
                  <span className={activeCase.case.written_to_graph ? 'text-emerald-700' : ''}>
                    {activeCase.case.written_to_graph
                      ? `written to the graph as ${activeCase.case.graph_case_id}`
                      : 'not written to a graph (DuckDB mirror)'}
                  </span>
                  <span>{activeCase.tool_calls} graph calls</span>
                  <button onClick={() => downloadCase(activeCase)}
                    className="ml-auto flex items-center gap-2 border border-line px-3 py-2
                               uppercase tracking-widest hover:text-ink hover:border-ink transition-colors">
                    <Download className="w-4 h-4" /> Download case file
                  </button>
                </div>

                <h3 className="font-serif text-3xl font-bold mb-8 border-b-2 border-ink pb-4">Actions Taken</h3>
                <div className="mb-16"><Ledger caseId={activeCase.case_id} refresh={ledgerTick} /></div>

                {/* Audit Log / Events */}
                {activeCase.events?.length > 0 && (
                  <>
                    <h3 className="font-serif text-3xl font-bold mb-8 border-b-2 border-line pb-4">System Event Log</h3>
                    <div className="bg-paper p-6 font-mono text-sm border border-line mb-16 space-y-4">
                      {activeCase.events.map((ev: any, i: number) => (
                        <div key={i} className="flex gap-4">
                          <span className="text-muted shrink-0">{ev.at.split('T')[1]}</span>
                          <span className="font-bold text-ink shrink-0 w-24">[{ev.kind}]</span>
                          <span className="text-ink/80">{ev.detail}</span>
                          {ev.by && <span className="text-muted shrink-0 ml-auto">{ev.by} · {ev.role}</span>}
                        </div>
                      ))}
                    </div>
                  </>
                )}

              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {prior && <PriorCase id={prior} onClose={() => setPrior(null)} />}
    </div>
  );
}

/** The exact JSON that goes in the submission, minus the console's own bookkeeping. */
function downloadCase(c: any) {
  const { source, closed, trigger, events, signals, suppressed, ring_cap, ...answer } = c;
  const blob = new Blob([JSON.stringify(answer, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${c.case_id}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}
