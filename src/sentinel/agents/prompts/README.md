# Agent system prompts

One `<agent>.md` per agent, loaded by `AgentBase.load_prompt(name)`:

- `yield.md` — Yield Agent (§4.1): argues for shipping, must cite functions.
- `adversary.md` — Adversary Agent (§4.2): every claim cites a SimulationMCP
  `trace_id` (Golden Rule #1).
- `arbitrator.md` — Arbitrator (§4.3): constraint solver, rejects unsourced
  vetoes, emits the `RiskProfile`.
- `lessons.md` — Lessons Agent (§4.4): frames retrieved memory as new
  constraints, not solutions (§6.1).
- `baseline.md` — Baseline Agent (§4.5): single-pass control, read-only.

Each prompt is the system prompt the model sees — edit here, not in code.
