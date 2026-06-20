# TODOS

Deferred work surfaced during the 2026-05-05 CEO review and later v1 release
reconciliation. See `~/.gstack/projects/berdyh-time-manager/ceo-plans/2026-05-05-temporal-knowledge-graph.md`
for the original plan and `docs/designs/time-manager-current-state-autoplan.md`
for the current-state reconciliation.

## Tier S (strategic / product decision before implementation)

- **[P2/M] Real eval-set companion repo.** Build `time-manager-evals` with at least 30 real and up to 20 synthetic examples before treating eval scores as product-quality release gates.

## v1.1

- **[P2/S] Subprocess-adapter shared helpers.** Before adding more CLI-backed LLM adapters, extract shared binary resolution, message serialization, subprocess timeout, JSON parsing, and error mapping from `CodexAdapter` and `ClaudeCodeAdapter`.
- **[P2/S] Subprocess adapter tool-call warnings.** Codex and Claude Code adapters currently expose `tool_calls=[]`; add a one-time warning if callers ask those adapters for tool-call behavior before it is implemented.
- **[P2/S] Conversational pattern reconciliation.** When new evidence contradicts a hardened pattern, debrief asks ("Sat family time was 3-5; last 4 weeks it's 4-6. Update?") instead of silently updating. The original plan assumed a silent pattern-audit surface, but current v1 does not ship `tm pattern audit`; implement the audit/revert surface together with conversational reconciliation if this becomes active.
- **[P2/M] Scheduler performance/conformance signals.** SchedulerAgent currently consumes process-mining variant signals only. Add performance and conformance signals if real usage shows they improve suggestions beyond the current variant/outcome context.
- **[P2/M] Additional direct/cloud LLM adapters.** Add OpenAI and Gemini only after subprocess-helper extraction. Keep the factory contract narrow and avoid adapter-specific behavior leaking into agents.
- **[P2/S] `OpenCodeAdapter`.** v1 ships Claude Code + Codex subprocess/CLI-backed adapters. Add OpenCode when its API or CLI surface is stable enough to depend on.
- **[P2/S] `OllamaAdapter`.** Local-LLM adapter via Ollama HTTP. Pairs with the user's eventual privacy upgrade. Architecture is ready (narrow internal contract); just needs the wire-format work.
- **[P3/S] Pydantic v2 conversion.** Current public engine returns use frozen dataclasses and manual validation. Revisit Pydantic v2 only once Anthropic's transitive pin is project-stable and the conversion buys enough validation or schema-generation value.
- **[P3/S] Nightly-batch packaging.** Daemon RPC handlers can be invoked by an external cron/systemd/launchd job, but this repo does not ship timer/plist files or a catchup command. Add packaging only when there is a real deployment target.
- **[P3/S] Incremental Kuzu update.** v1 rebuilds Kuzu from SQLite on schema change or integrity-mismatch. If rebuild ever exceeds 30s wall (currently <5s at v1 scale, would happen ~5M+ events), switch to incremental update and expose the timing through a future metrics/logging surface.

## v2 (after 30+ days of consistent v1 use)

- **[P2/M] Live calendar sync.** `tm capture calendar` imports local `.ics` files. Read-only CalDAV/iCloud/Google sync remains separate integration work.
- **[P2/M] Live Telegram bot.** `tm capture telegram` imports Telegram JSON exports. Polling/webhook bot delivery, allowlists, and chat UX remain separate integration work.
- **[P3/S] Voice transcription.** `tm capture voice` stores already-transcribed voice notes. Audio upload + Whisper/local transcription remains separate integration work.
- **[P3/S] Daily morning briefing.** 5am push: "Here's your day. Calendar has X. Based on patterns, deep work 9-11. Auth bug from yesterday — slot or defer?" Only fires if there's a hardened pattern relevant to today.
- **[P3/S] Friction-aware extraction.** Detect terse / venting / focused user states and adapt the debrief depth. Reduces friction on bad days.
- **[P3/M] Goal hierarchy.** Tasks tied to goals tied to higher intents. Scheduler can ask "this serves project X — still a priority?"
- **[P3/S] Weekly reflection (Sunday digest).** "Here's what I extracted this week. Any of this wrong? Any patterns to update?" Audit and correction surface.

## v3 (only if a real causal hypothesis demands it)

- **Browser history + phone screen-time integration.** Privacy posture must be re-evaluated; the Telegram trade-off may not survive at this data sensitivity level.
- **Formal causal inference layer (DoWhy or similar).** Re-evaluate after 6+ months of v1+v2 data. May or may not produce useful signal at N=1.

## Notes

- Effort estimates use the legend: S = ≤1 day CC, M = 1-3 days CC, L = 3-10 days CC.
- Priorities: P1 = blocks v1 ship, P2 = strong v1.1 candidate, P3 = backlog.
- Each item should be re-evaluated when its tier becomes active. Some may turn out to be unnecessary once v1 is in daily use.
