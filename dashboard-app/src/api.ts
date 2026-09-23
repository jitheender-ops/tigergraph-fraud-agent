// The console's whole surface. Every call re-runs or records something on the agent
// side; none of it edits an answer file in the browser.
export type Diff = Record<string, [unknown, unknown]>;

// The analyst token every change needs. Session-scoped, so it dies with the tab; storage
// can be blocked (private windows), so every access is guarded.
export const token = {
  get: (): string => { try { return sessionStorage.getItem('analystToken') ?? ''; } catch { return ''; } },
  set: (t: string) => { try { sessionStorage.setItem('analystToken', t); } catch { /* memory only */ } },
};
export const authHeaders = (): Record<string, string> =>
  token.get() ? { 'X-Analyst-Token': token.get() } : {};

async function call<T>(path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? headers
      : { 'content-type': 'application/json', ...authHeaders(), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.text()).slice(0, 300));
  return res.json() as Promise<T>;
}

export const api = {
  cases: () => call<any[]>('/cases'),
  case: (id: string) => call<any>(`/case/${id}`),
  challenge: (id: string, text: string) =>
    call<{ matched: string[]; case?: any; changed?: Diff; note?: string }>(
      `/case/${id}/challenge`, { text }),
  deepen: (id: string, cap: number) => call<{ case: any; changed: Diff }>(`/case/${id}/deepen`, { cap }),
  reset: (id: string) => call<{ restored?: string[]; case?: any; changed?: Diff; note?: string }>(
    `/case/${id}/reset`, {}),
  stepup: (id: string) => call<{ passed: boolean; case: any; changed: Diff }>(`/case/${id}/stepup`, {}),
  decide: (id: string, decision: 'approve' | 'override', action?: string, note?: string) =>
    call<any>(`/case/${id}/decision`, { decision, action, note }),
  close: (id: string, outcome: 'confirmed_fraud' | 'cleared', note: string) =>
    call<any>(`/case/${id}/close`, { outcome, note }),
  blacklist: (device_profile: string, note: string) =>
    call<any>('/device/blacklist', { device_profile, note }),
  // the approval tier comes from the token, server side; the UI only carries it
  release: (id: string, action: string, approver: string, token: string) =>
    call<any>(`/case/${id}/release`, { action, approver }, { 'X-Approver-Token': token }),
  reply: (id: string, type: string, outcome: 'confirmed' | 'denied' | 'no_reply', note: string) =>
    call<any>(`/case/${id}/reply`, { type, outcome, note }),
  open: (card_id: string, txn_id: number, trigger_type: string, trigger_text: string) =>
    call<any>('/cases', { card_id, txn_id, trigger_type, trigger_text }),
};

// Every action the policy knows, for the override picker. Routing is decided server
// side by policy.route_for, never here — the UI only names the action.
export const ACTIONS = [
  'ALLOW_TRANSACTION', 'DECLINE_TRANSACTION', 'MONITOR_CARD', 'MONITOR_CONNECTED_CARDS',
  'WARN_CUSTOMER', 'VERIFY_WITH_CUSTOMER', 'STEP_UP_AUTH', 'BLOCK_CARD', 'BLOCK_ALL_CARDS',
  'GENERATE_REPORT', 'CREATE_CASE', 'FILE_REPORT', 'ESCALATE_TO_ANALYST', 'CLOSE_NO_FRAUD',
];
