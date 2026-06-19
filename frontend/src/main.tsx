import React from "react";
import ReactDOM from "react-dom/client";
import {
  Activity,
  Bot,
  CalendarClock,
  CircleDot,
  Database,
  Download,
  Gauge,
  LockKeyhole,
  MessageSquareText,
  Play,
  Radar,
  RefreshCw,
  Shield,
  Target,
  Upload,
  TimerReset,
  Trash2
} from "lucide-react";
import "./styles.css";

type StatusPayload = {
  db_path: string;
  socket_path: string;
  api_token: string;
  daemon: { alive: boolean; detail: string };
  cost: { monthly_total_usd: number | null; monthly_cap_usd: number | null };
  selected_agent: string;
  selected_model: string | null;
  encryption: { available: boolean; status: string };
  generated_at: string;
};

type Agent = {
  agent_id: string;
  label: string;
  backend: string | null;
  command: string | null;
  routeable: boolean;
  installed: boolean;
  version: string | null;
  selected: boolean;
  healthy: boolean;
  status: string;
  notes: string;
};

type DashboardPayload = {
  events: number;
  case_dates: string[];
  transcripts: number;
  privacy_actions: number;
  active_goals: Goal[];
  suggestions: Suggestion[];
  avg_outcome: number | null;
  top_activities: Array<{ activity: string; count: number }>;
};

type Goal = {
  goal_id: string;
  name: string;
  status: string;
  priority: number | null;
};

type Suggestion = {
  suggestion_id: string;
  recommended_action: string;
  case_date: string;
  predicted_outcome_delta: number;
  actual_outcome: number | null;
};

type NowPayload = {
  directive: string;
  active_goal: Goal | null;
  latest_case_date: string | null;
  outcome: {
    outcome_score: number;
    planned_tasks_completed: number;
    planned_tasks_total: number;
    advancing_goal_event_count: number;
  } | null;
  suggestion: Suggestion | null;
  recent_events: Array<{ event_id: string; activity: string; timestamp: string }>;
  selected_agent: { agent_id: string; backend: string | null; model: string | null };
  schedule_delta: { label: string; event_count: number; activities?: string[] };
};

type Capabilities = Record<string, boolean | string>;

type ApiState = {
  status: StatusPayload | null;
  agents: Agent[];
  dashboard: DashboardPayload | null;
  now: NowPayload | null;
  capabilities: Capabilities | null;
};

type StatusTone = "good" | "warn" | "neutral";

type StatusPillProps = {
  icon: React.ReactNode;
  label: string;
  value: string;
  tone: StatusTone;
};

type ExportPayload = {
  tables: Record<string, unknown[]>;
  rows_exported: number;
};

const API = {
  status: "/api/status",
  agents: "/api/agents",
  dashboard: "/api/dashboard",
  now: "/api/now",
  capabilities: "/api/capabilities",
  selectAgent: "/api/agents/select",
  debrief: "/api/debrief",
  export: "/api/export",
  backup: "/api/backup",
  captureTelegram: "/api/capture/telegram",
  captureCalendar: "/api/capture/calendar",
  captureVoice: "/api/capture/voice",
  privacyRedact: "/api/privacy/redact",
  privacyForget: "/api/privacy/forget",
  reextract: "/api/reextract"
} as const;

const RAIL_ITEMS = [
  { href: "#now", label: "Now", Icon: Gauge },
  { href: "#debrief", label: "Debrief", Icon: MessageSquareText },
  { href: "#plan", label: "Plan", Icon: CalendarClock },
  { href: "#agents", label: "Agents", Icon: Bot },
  { href: "#data", label: "Data", Icon: Database }
] as const;

const initialState: ApiState = {
  status: null,
  agents: [],
  dashboard: null,
  now: null,
  capabilities: null
};

function tokenHeaders(apiToken: string | null | undefined): Record<string, string> {
  return apiToken ? { "x-tm-web-token": apiToken } : {};
}

async function getJson<T>(
  path: string,
  apiToken?: string | null | undefined
): Promise<T> {
  const response = await fetch(path, { headers: tokenHeaders(apiToken) });
  if (!response.ok) {
    throw new Error(`${path} ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function postJson<T>(
  path: string,
  payload: unknown,
  apiToken: string | null | undefined
): Promise<T> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  Object.assign(headers, tokenHeaders(apiToken));
  const response = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(payload)
  });
  const data = (await response.json()) as T;
  if (!response.ok) {
    const detail = data as { detail?: string; error?: string };
    throw new Error(detail.detail ?? detail.error ?? `${path} ${response.status}`);
  }
  return data;
}

async function loadApiState(): Promise<ApiState> {
  const status = await getJson<StatusPayload>(API.status);
  const apiToken = status.api_token;
  const [agents, dashboard, now, capabilities] = await Promise.all([
    getJson<{ agents: Agent[] }>(API.agents, apiToken),
    getJson<DashboardPayload>(API.dashboard, apiToken),
    getJson<NowPayload>(API.now, apiToken),
    getJson<Capabilities>(API.capabilities, apiToken)
  ]);
  return {
    status,
    agents: agents.agents,
    dashboard,
    now,
    capabilities
  };
}

function errorMessage(err: unknown, fallback: string) {
  return err instanceof Error ? err.message : fallback;
}

function App() {
  const [state, setState] = React.useState<ApiState>(initialState);
  const [error, setError] = React.useState<string | null>(null);
  const [actionMessage, setActionMessage] = React.useState<string | null>(null);

  const refresh = React.useCallback(async () => {
    try {
      setState(await loadApiState());
      setError(null);
    } catch (err) {
      setError(errorMessage(err, "API unavailable"));
    }
  }, []);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  const selectedAgent =
    state.agents.find((agent) => agent.selected) ?? state.agents[0] ?? null;
  const apiToken = state.status?.api_token;

  async function selectAgent(agentId: string) {
    await postJson(API.selectAgent, { agent_id: agentId }, apiToken);
    await refresh();
  }

  async function runAction(label: string, action: () => Promise<void>) {
    try {
      await action();
      setActionMessage(label);
      await refresh();
      setError(null);
    } catch (err) {
      setError(errorMessage(err, "Action failed"));
    }
  }

  function postAction(label: string, path: string, payload: unknown) {
    return runAction(label, async () => {
      await postJson(path, payload, apiToken);
    });
  }

  function exportData() {
    return runAction("Export prepared", async () => {
      const payload = await getJson<ExportPayload>(API.export, apiToken);
      downloadJson("tm-export.json", payload.tables);
    });
  }

  const statusPills: StatusPillProps[] = [
    {
      icon: <TimerReset size={16} />,
      label: "Daemon",
      value: state.status?.daemon.alive ? "alive" : "down",
      tone: state.status?.daemon.alive ? "good" : "warn"
    },
    {
      icon: <Bot size={16} />,
      label: "Agent",
      value: selectedAgent?.label ?? state.status?.selected_agent ?? "none",
      tone: selectedAgent?.healthy ? "good" : "warn"
    },
    {
      icon: <Activity size={16} />,
      label: "Spend",
      value: formatCost(state.status),
      tone: "neutral"
    },
    {
      icon: <LockKeyhole size={16} />,
      label: "Storage",
      value: state.status?.encryption.available ? "encrypted" : "local",
      tone: state.status?.encryption.available ? "good" : "neutral"
    }
  ];

  return (
    <main className="shell">
      <nav className="rail" aria-label="Primary">
        <div className="mark">
          <CircleDot size={18} />
          <span>tm</span>
        </div>
        {RAIL_ITEMS.map(({ href, label, Icon }) => (
          <a
            key={href}
            className={href === "#now" ? "rail-item active" : "rail-item"}
            href={href}
            title={label}
          >
            <Icon size={18} />
            <span>{label}</span>
          </a>
        ))}
      </nav>

      <section className="workspace">
        <header className="status-strip">
          {statusPills.map((pill) => (
            <StatusPill key={pill.label} {...pill} />
          ))}
          <button className="icon-button" onClick={() => void refresh()} title="Refresh">
            <RefreshCw size={17} />
          </button>
        </header>

        {error ? <div className="error-band">{error}</div> : null}
        {actionMessage ? <div className="ok-band">{actionMessage}</div> : null}

        <section className="grid" id="now">
          <DirectivePanel now={state.now} selectedAgent={selectedAgent} />
          <AgentDeck agents={state.agents} onSelect={selectAgent} />
          <TimelinePanel now={state.now} />
          <DataPanel
            dashboard={state.dashboard}
            capabilities={state.capabilities}
            onExport={exportData}
            onBackup={(outputPath) =>
              postAction("Backup written", API.backup, {
                output_path: outputPath,
                overwrite: false
              })
            }
          />
          <CapturePanel
            onTelegram={(content) =>
              postAction("Telegram capture imported", API.captureTelegram, { content })
            }
            onCalendar={(content) =>
              postAction("Calendar capture imported", API.captureCalendar, { content })
            }
            onVoice={(caseDate, transcript) =>
              postAction("Voice transcript captured", API.captureVoice, {
                case_date: caseDate,
                transcript
              })
            }
          />
          <PrivacyPanel
            onRedact={(payload) =>
              postAction("Privacy redaction complete", API.privacyRedact, payload)
            }
            onForget={(payload) =>
              postAction("Privacy forget complete", API.privacyForget, payload)
            }
          />
          <DebriefPanel
            now={state.now}
            onDebrief={(caseDate, transcript) =>
              postAction("Debrief complete", API.debrief, {
                case_date: caseDate,
                transcript
              })
            }
            onReextract={(caseDate, transcript) =>
              postAction("Reextract requested", API.reextract, {
                case_date: caseDate,
                transcript: transcript || undefined
              })
            }
          />
        </section>
      </section>
    </main>
  );
}

function StatusPill({
  icon,
  label,
  value,
  tone
}: StatusPillProps) {
  return (
    <div className={`status-pill ${tone}`}>
      {icon}
      <span className="status-label">{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function DirectivePanel({
  now,
  selectedAgent
}: {
  now: NowPayload | null;
  selectedAgent: Agent | null;
}) {
  const outcome = now?.outcome?.outcome_score ?? 0;
  return (
    <article className="panel directive-panel">
      <div className="panel-kicker">
        <Radar size={16} />
        <span>{now?.latest_case_date ?? "today"}</span>
      </div>
      <h1>{now?.directive ?? "Reading local state"}</h1>
      <div className="directive-meta">
        <span>{now?.active_goal?.name ?? "No active goal"}</span>
        <span>Outcome {outcome}/2</span>
        <span>{selectedAgent?.label ?? "No agent"}</span>
      </div>
      <div className="action-row">
        <button className="primary-action" title="Start directive">
          <Play size={17} />
          <span>Start</span>
        </button>
        <button className="secondary-action" title="Mark done">
          <Target size={17} />
          <span>Done</span>
        </button>
      </div>
      <div className="evidence">
        <div>
          <span>Tasks</span>
          <strong>
            {now?.outcome
              ? `${now.outcome.planned_tasks_completed}/${now.outcome.planned_tasks_total}`
              : "0/0"}
          </strong>
        </div>
        <div>
          <span>Goal events</span>
          <strong>{now?.outcome?.advancing_goal_event_count ?? 0}</strong>
        </div>
        <div>
          <span>Trace</span>
          <strong>{now?.schedule_delta.event_count ?? 0}</strong>
        </div>
      </div>
    </article>
  );
}

function AgentDeck({
  agents,
  onSelect
}: {
  agents: Agent[];
  onSelect: (agentId: string) => Promise<void>;
}) {
  return (
    <article className="panel agent-panel" id="agents">
      <div className="panel-title">
        <Bot size={17} />
        <h2>Agent Switchboard</h2>
      </div>
      <div className="agent-list">
        {agents.map((agent) => (
          <button
            key={agent.agent_id}
            className={`agent-row ${agent.selected ? "selected" : ""}`}
            disabled={!agent.routeable || !agent.installed}
            onClick={() => void onSelect(agent.agent_id)}
            title={agent.notes}
          >
            <span className={`dot ${agent.healthy ? "good" : "warn"}`} />
            <span>
              <strong>{agent.label}</strong>
              <small>{agent.version ?? agent.status}</small>
            </span>
          </button>
        ))}
      </div>
    </article>
  );
}

function TimelinePanel({ now }: { now: NowPayload | null }) {
  const events = now?.recent_events ?? [];
  return (
    <article className="panel timeline-panel" id="plan">
      <div className="panel-title">
        <CalendarClock size={17} />
        <h2>Trace Spine</h2>
      </div>
      <div className="trace-list">
        {events.length === 0 ? (
          <div className="empty-line">No events</div>
        ) : (
          events.map((event) => (
            <div className="trace-row" key={event.event_id}>
              <span>{compactTime(event.timestamp)}</span>
              <strong>{event.activity}</strong>
            </div>
          ))
        )}
      </div>
    </article>
  );
}

function DataPanel({
  dashboard,
  capabilities,
  onExport,
  onBackup
}: {
  dashboard: DashboardPayload | null;
  capabilities: Capabilities | null;
  onExport: () => void;
  onBackup: (outputPath: string) => void;
}) {
  const [backupPath, setBackupPath] = React.useState("tm-backup.db");
  return (
    <article className="panel data-panel" id="data">
      <div className="panel-title">
        <Database size={17} />
        <h2>Local Ledger</h2>
      </div>
      <div className="metric-grid">
        <Metric label="Events" value={String(dashboard?.events ?? 0)} />
        <Metric label="Days" value={String(dashboard?.case_dates.length ?? 0)} />
        <Metric label="Transcripts" value={String(dashboard?.transcripts ?? 0)} />
        <Metric label="Privacy" value={String(dashboard?.privacy_actions ?? 0)} />
        <Metric
          label="Outcome"
          value={dashboard?.avg_outcome == null ? "n/a" : dashboard.avg_outcome.toFixed(1)}
        />
      </div>
      <div className="activity-bars">
        {(dashboard?.top_activities ?? []).map((item) => (
          <div className="bar-row" key={item.activity}>
            <span>{item.activity}</span>
            <i style={{ width: `${Math.min(100, item.count * 18)}%` }} />
            <strong>{item.count}</strong>
          </div>
        ))}
      </div>
      <div className="tool-row">
        <ToolState icon={<Download size={15} />} label="Export" on={Boolean(capabilities?.export)} />
        <ToolState icon={<Shield size={15} />} label="Privacy" on={Boolean(capabilities?.privacy)} />
        <ToolState icon={<RefreshCw size={15} />} label="Trend" on={Boolean(capabilities?.variants_trend)} />
      </div>
      <div className="action-row compact data-actions">
        <button className="secondary-action" onClick={onExport} title="Download JSON export">
          <Download size={16} />
          <span>Export</span>
        </button>
        <input
          className="path-input"
          value={backupPath}
          onChange={(event) => setBackupPath(event.target.value)}
          aria-label="Backup output path"
        />
        <button
          className="secondary-action"
          onClick={() => onBackup(backupPath)}
          title="Write SQLite backup"
        >
          <Database size={16} />
          <span>Backup</span>
        </button>
      </div>
    </article>
  );
}

function CapturePanel({
  onTelegram,
  onCalendar,
  onVoice
}: {
  onTelegram: (content: string) => void;
  onCalendar: (content: string) => void;
  onVoice: (caseDate: string, transcript: string) => void;
}) {
  const [voiceCaseDate, setVoiceCaseDate] = React.useState(todayIsoDate());
  const [voiceTranscript, setVoiceTranscript] = React.useState("");
  return (
    <article className="panel capture-panel">
      <div className="panel-title">
        <Upload size={17} />
        <h2>Intake</h2>
      </div>
      <div className="file-actions">
        <FileButton label="Telegram JSON" accept=".json,application/json" onFile={onTelegram} />
        <FileButton label="Calendar ICS" accept=".ics,text/calendar" onFile={onCalendar} />
      </div>
      <label className="field-label">
        Voice case date
        <input
          value={voiceCaseDate}
          onChange={(event) => setVoiceCaseDate(event.target.value)}
        />
      </label>
      <textarea
        aria-label="Voice transcript"
        placeholder="Already-transcribed voice note"
        rows={5}
        value={voiceTranscript}
        onChange={(event) => setVoiceTranscript(event.target.value)}
      />
      <button
        className="secondary-action"
        onClick={() => onVoice(voiceCaseDate, voiceTranscript)}
        title="Store voice transcript"
      >
        <MessageSquareText size={16} />
        <span>Capture Voice</span>
      </button>
    </article>
  );
}

function PrivacyPanel({
  onRedact,
  onForget
}: {
  onRedact: (payload: Record<string, string>) => void;
  onForget: (payload: Record<string, string>) => void;
}) {
  const [selectorKind, setSelectorKind] = React.useState<"case_date" | "event_id">("case_date");
  const [selectorValue, setSelectorValue] = React.useState(todayIsoDate());
  const [replacement, setReplacement] = React.useState("[redacted]");
  const payload = () => ({ [selectorKind]: selectorValue, replacement });
  return (
    <article className="panel privacy-panel">
      <div className="panel-title">
        <Shield size={17} />
        <h2>Privacy Lock</h2>
      </div>
      <div className="segmented">
        <button
          className={selectorKind === "case_date" ? "active" : ""}
          onClick={() => setSelectorKind("case_date")}
        >
          Case date
        </button>
        <button
          className={selectorKind === "event_id" ? "active" : ""}
          onClick={() => setSelectorKind("event_id")}
        >
          Event ID
        </button>
      </div>
      <input
        value={selectorValue}
        onChange={(event) => setSelectorValue(event.target.value)}
        aria-label="Privacy selector"
      />
      <input
        value={replacement}
        onChange={(event) => setReplacement(event.target.value)}
        aria-label="Replacement text"
      />
      <div className="action-row compact">
        <button className="secondary-action" onClick={() => onRedact(payload())}>
          <Shield size={16} />
          <span>Redact</span>
        </button>
        <button className="danger-action" onClick={() => onForget(payload())}>
          <Trash2 size={16} />
          <span>Forget</span>
        </button>
      </div>
    </article>
  );
}

function FileButton({
  label,
  accept,
  onFile
}: {
  label: string;
  accept: string;
  onFile: (content: string) => void;
}) {
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  return (
    <>
      <button
        className="secondary-action"
        onClick={() => inputRef.current?.click()}
        title={`Import ${label}`}
      >
        <Upload size={16} />
        <span>{label}</span>
      </button>
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (!file) {
            return;
          }
          void file.text().then(onFile);
          event.target.value = "";
        }}
      />
    </>
  );
}

function DebriefPanel({
  now,
  onDebrief,
  onReextract
}: {
  now: NowPayload | null;
  onDebrief: (caseDate: string, transcript: string) => void;
  onReextract: (caseDate: string, transcript: string) => void;
}) {
  const [replacementTranscript, setReplacementTranscript] = React.useState("");
  const caseDate = now?.latest_case_date ?? todayIsoDate();
  return (
    <article className="panel debrief-panel" id="debrief">
      <div className="panel-title">
        <MessageSquareText size={17} />
        <h2>Debrief Console</h2>
      </div>
      <textarea
        aria-label="Debrief transcript"
        placeholder="End-of-day transcript"
        rows={7}
        value={replacementTranscript}
        onChange={(event) => setReplacementTranscript(event.target.value)}
      />
      <div className="action-row compact">
        <button
          className="secondary-action"
          title="Run debrief"
          onClick={() => onDebrief(caseDate, replacementTranscript)}
        >
          <Play size={16} />
          <span>Run</span>
        </button>
        <button
          className="secondary-action"
          title="Replay retained transcript"
          onClick={() => onReextract(caseDate, replacementTranscript)}
        >
          <RefreshCw size={16} />
          <span>Reextract</span>
        </button>
        <span className="case-date">{caseDate}</span>
      </div>
    </article>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ToolState({
  icon,
  label,
  on
}: {
  icon: React.ReactNode;
  label: string;
  on: boolean;
}) {
  return (
    <span className={`tool-state ${on ? "on" : ""}`}>
      {icon}
      {label}
    </span>
  );
}

function compactTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value.slice(0, 10);
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatCost(status: StatusPayload | null) {
  if (!status || status.cost.monthly_total_usd == null || status.cost.monthly_cap_usd == null) {
    return "$0";
  }
  return `$${status.cost.monthly_total_usd.toFixed(2)} / ${status.cost.monthly_cap_usd.toFixed(0)}`;
}

function todayIsoDate() {
  return new Date().toISOString().slice(0, 10);
}

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: "application/json"
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
