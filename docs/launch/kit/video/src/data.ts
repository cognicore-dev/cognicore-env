// Text from one real benchmark run (scripts/bench_realworld.py, arm x_cognicore_hooksim, task billing).
// Long replies are cut to their first lines; nothing is reworded.

export const SESSION1_PROMPT =
  "Ticket BILL-311: customers in Germany see invoice totals that are off by one cent. " +
  "We decided to compute totals with decimal.Decimal and ROUND_HALF_UP instead of round(), " +
  "because finance requires commercial rounding. Do NOT change format_eur in this session: " +
  "that is a follow-up for next time. It must produce German formatting, like '12,34 €'. " +
  "FYI the staging deploy is on Thursday and the reviewer is Anika.";

export const SESSION1_REPLY = [
  "I fixed `total_with_tax` in `billing.py` and added a DE test. `python -m unittest` passes both tests.",
  "Fix: `total_with_tax` now uses `Decimal` with `ROUND_HALF_UP`, rounded to 0.01.",
  "`format_eur`: I left it alone, as you asked.",
];

// The brief CogniCore's session-start hook produced from session 1 (457 tokens), first lines.
export const BRIEF = [
  { label: "Goal", text: "Ticket BILL-311: customers in Germany see invoice totals that are off by one cent." },
  { label: "Open task", text: "It must produce German formatting with a comma decimal separator, like '12,34 €'." },
  { label: "Decision", text: "Decimal and ROUND_HALF_UP instead of round() (why: finance requires commercial rounding)" },
  { label: "Fact", text: "FYI the staging deploy is on Thursday and the reviewer is Anika." },
];
export const BRIEF_TOKENS = 457;

export const SESSION2_PROMPT = "Continue where the previous session on this repository left off.";

export const SESSION2_REPLY =
  "Updated `format_eur` to produce German formatting, and added a test for `12.34` → `12,34 €`. " +
  "`python -m unittest` passes all 3 tests.";

// From the same reply's answers block.
export const RECALLED = ["BILL-311", "ROUND_HALF_UP", "12,34 €", "Anika", "Thursday"];

export const INSTALL = 'pipx install "cognicore-env[mcp]" && cognicore install';
export const REPO = "github.com/cognicore-dev/cognicore-env";

// claude.ai picking the work up through the CogniCore connector. In a real test
// (2026-11-03) claude.ai called "Resume from past sessions" on its own when asked
// what happened in Claude Code. The answer below is built only from the brief above.
export const WEB_PROMPT = "What did I do in my last Claude Code session?";
export const WEB_TOOL = "Used CogniCore: Resume from past sessions";
export const WEB_ANSWER = [
  "You worked on ticket BILL-311: German invoice totals were off by one cent.",
  "Totals now use Decimal with ROUND_HALF_UP, because finance requires commercial rounding.",
  "Still open: format_eur must produce German formatting, like '12,34 €'.",
  "Staging deploy is on Thursday; the reviewer is Anika.",
];

// Measured numbers (docs/transfer-audit.md and real Claude Code transcripts on one machine).
export const STATS = [
  { value: "~1,000", unit: "tokens", label: "brief instead of 1.36M tokens of history", note: "largest real session: 996 vs 1,361,772" },
  { value: "228", unit: "tokens/turn", label: "plugin overhead, down from 1,240", note: "one search tool; hooks save and resume" },
  { value: "2.4–4.1×", unit: "fewer tokens", label: "than claude-mem, same follow-through", note: "Claude Code → Claude Code and → Codex" },
  { value: "0", unit: "model calls", label: "to save a session", note: "rule-based; ready in ~1 second" },
];

export const LOGIN = "cognicore login   # sync with claude.ai, Desktop and mobile";
