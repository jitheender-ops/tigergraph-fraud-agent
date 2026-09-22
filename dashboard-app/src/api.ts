// The console's whole surface. Every call re-runs or records something on the agent
// side; none of it edits an answer file in the browser.
export type Diff = Record<string, [unknown, unknown]>;

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
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
};

// Every action the policy knows, for the override picker. Routing is decided server
// side by policy.route_for, never here — the UI only names the action.
export const ACTIONS = [
  'ALLOW_TRANSACTION', 'DECLINE_TRANSACTION', 'MONITOR_CARD', 'MONITOR_CONNECTED_CARDS',
  'WARN_CUSTOMER', 'VERIFY_WITH_CUSTOMER', 'STEP_UP_AUTH', 'BLOCK_CARD', 'BLOCK_ALL_CARDS',
  'GENERATE_REPORT', 'CREATE_CASE', 'FILE_REPORT', 'ESCALATE_TO_ANALYST', 'CLOSE_NO_FRAUD',
];
