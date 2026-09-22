# The analyst console

The front end of the fraud investigation agent. It is not a report viewer: three of the
things it does re-run the investigation on the server, so a human can withdraw a premise
and watch the probability move.

```bash
../run.sh          # from the repo root: API on :8000, this on :5180
```

It needs `server.py` running — the case data comes from `/api/cases`, not from a file in
`public/`. Started on its own with `npm run dev`, it will say so rather than render an
empty page.

| | |
|---|---|
| `src/App.tsx` | the desk: queue, case detail, keyboard navigation |
| `src/components/Steering.tsx` | challenge, look wider, step-up, undo |
| `src/components/Decide.tsx` | approve, override, close, blacklist |
| `src/components/Views.tsx` | the log-odds waterfall, the ego-network, the episode timeline, a prior case |
| `src/api.ts` | every endpoint, in one place |

The waterfall is the one worth understanding. `fraud_probability` is a logistic over
summed log-odds, and the submission spec fixes the shape of `evidence` with no room for a
signal's name or weight — so the API returns those separately and the waterfall draws
each contribution and the running probability after it. That is what makes the challenge
box honest: you see which bar disappears.

The port is pinned to 5180 in `vite.config.ts`. Vite silently moves to the next free port
otherwise, and a dev proxy on a port the README does not name is a confusing ten minutes.
