"""Local coding-agent discovery and UI selection helpers.

The web UI treats coding agents as local runtimes with their own auth,
subscriptions, model defaults, and tool configuration. This module only probes
availability and stores the UI's selected backend; it does not collect secrets.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tm._paths import default_data_dir
from tm.llm.factory import BACKEND_ENV

__all__ = [
    "AgentDefinition",
    "AgentStatus",
    "ROUTEABLE_BACKENDS",
    "default_selected_agent",
    "default_agent_config_path",
    "load_agent_config",
    "probe_agents",
    "save_selected_agent",
]

AGENT_CONFIG_FILENAME = "web-config.json"
ROUTEABLE_BACKENDS = frozenset(
    {"anthropic", "codex", "claude-code", "gemini", "kimchi"}
)
PREFERRED_LOCAL_AGENTS = ("codex", "claude-code", "gemini", "kimchi")


@dataclass(frozen=True)
class AgentDefinition:
    """Static metadata for a selectable local agent."""

    agent_id: str
    label: str
    backend: str | None
    command: str | None
    version_args: tuple[str, ...]
    routeable: bool
    notes: str


@dataclass(frozen=True)
class AgentStatus:
    """Runtime probe result for one local agent."""

    agent_id: str
    label: str
    backend: str | None
    command: str | None
    routeable: bool
    installed: bool
    version: str | None
    selected: bool
    healthy: bool
    status: str
    notes: str
    gateway: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


AGENT_DEFINITIONS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        agent_id="codex",
        label="Codex",
        backend="codex",
        command="codex",
        version_args=("--version",),
        routeable=True,
        notes="Uses the local Codex CLI account and configuration.",
    ),
    AgentDefinition(
        agent_id="claude-code",
        label="Claude Code",
        backend="claude-code",
        command="claude",
        version_args=("--version",),
        routeable=True,
        notes="Uses Claude Code in bare print mode with TM_LLM_API_KEY.",
    ),
    AgentDefinition(
        agent_id="gemini",
        label="Gemini",
        backend="gemini",
        command="gemini",
        version_args=("--version",),
        routeable=True,
        notes="Uses the local Gemini CLI account and default model.",
    ),
    AgentDefinition(
        agent_id="kimchi",
        label="Kimchi",
        backend="kimchi",
        command="kimchi",
        version_args=("--version",),
        routeable=True,
        notes="Uses the local Kimchi CLI account and default model.",
    ),
    AgentDefinition(
        agent_id="anthropic",
        label="Anthropic API",
        backend="anthropic",
        command=None,
        version_args=(),
        routeable=True,
        notes="Uses TM_LLM_API_KEY directly through the Anthropic SDK.",
    ),
    AgentDefinition(
        agent_id="openclaw",
        label="OpenClaw",
        backend=None,
        command="openclaw",
        version_args=("--version",),
        routeable=False,
        notes="Tracked as local orchestration status until its gateway is healthy.",
    ),
)
AGENT_DEFINITIONS_BY_ID = {
    definition.agent_id: definition for definition in AGENT_DEFINITIONS
}


def default_agent_config_path() -> Path:
    """Return the persisted web UI config path, creating the data dir lazily."""

    return default_data_dir() / AGENT_CONFIG_FILENAME


def load_agent_config(config_path: Path | None = None) -> dict[str, Any]:
    """Return persisted UI config, or an empty config when absent/malformed."""

    path = config_path or default_agent_config_path()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_selected_agent(
    agent_id: str,
    *,
    model: str | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Persist selected UI agent under the XDG data directory."""

    definition = _definition(agent_id)
    if definition.backend not in ROUTEABLE_BACKENDS:
        raise ValueError(f"agent is not routeable by tm yet: {agent_id!r}")

    path = config_path or default_agent_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config = load_agent_config(path)
    config["selected_agent"] = agent_id
    if model is not None:
        config["selected_model"] = model
    path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    try:
        os.chmod(path, 0o600)
    except PermissionError:
        pass
    return config


def default_selected_agent(config_path: Path | None = None) -> str:
    """Resolve selected agent from config, env, and installed local CLIs."""

    config = load_agent_config(config_path)
    selected = config.get("selected_agent")
    if isinstance(selected, str) and selected:
        return selected

    env_backend = os.environ.get("TM_WEB_AGENT") or os.environ.get(BACKEND_ENV)
    if env_backend in ROUTEABLE_BACKENDS:
        return str(env_backend)

    for candidate in PREFERRED_LOCAL_AGENTS:
        definition = _definition(candidate)
        if definition.command and shutil.which(definition.command):
            return candidate
    return "anthropic"


def probe_agents(config_path: Path | None = None) -> list[dict[str, Any]]:
    """Probe all known local agents and return JSON-ready status rows."""

    selected = default_selected_agent(config_path)
    statuses = [
        _probe_definition(definition, selected=selected)
        for definition in AGENT_DEFINITIONS
    ]
    return [status.to_dict() for status in statuses]


def _definition(agent_id: str) -> AgentDefinition:
    try:
        return AGENT_DEFINITIONS_BY_ID[agent_id]
    except KeyError as exc:
        raise ValueError(f"unknown agent_id: {agent_id!r}") from exc


def _probe_definition(definition: AgentDefinition, *, selected: str) -> AgentStatus:
    if definition.command is None:
        configured = bool(os.environ.get("TM_LLM_API_KEY"))
        return _agent_status(
            definition,
            selected=selected,
            command=None,
            installed=True,
            version=None,
            healthy=configured,
            status="configured" if configured else "TM_LLM_API_KEY not set",
        )

    binary = shutil.which(definition.command)
    if binary is None:
        return _agent_status(
            definition,
            selected=selected,
            command=definition.command,
            installed=False,
            version=None,
            healthy=False,
            status="not installed",
        )

    version = _read_version(binary, definition.version_args)
    gateway = (
        _probe_openclaw_gateway(binary) if definition.agent_id == "openclaw" else None
    )
    healthy = True
    status = "ready"
    if definition.agent_id == "openclaw":
        healthy = bool(gateway and gateway.get("healthy"))
        status = "gateway healthy" if healthy else "gateway unavailable"

    return _agent_status(
        definition,
        selected=selected,
        command=binary,
        installed=True,
        version=version,
        healthy=healthy,
        status=status,
        gateway=gateway,
    )


def _agent_status(
    definition: AgentDefinition,
    *,
    selected: str,
    command: str | None,
    installed: bool,
    version: str | None,
    healthy: bool,
    status: str,
    gateway: dict[str, Any] | None = None,
) -> AgentStatus:
    return AgentStatus(
        agent_id=definition.agent_id,
        label=definition.label,
        backend=definition.backend,
        command=command,
        routeable=definition.routeable,
        installed=installed,
        version=version,
        selected=definition.agent_id == selected,
        healthy=healthy,
        status=status,
        notes=definition.notes,
        gateway=gateway,
    )


def _read_version(binary: str, args: tuple[str, ...]) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - binary comes from PATH probe
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr or "").strip()
    return text.splitlines()[0] if text else None


def _probe_openclaw_gateway(binary: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(  # noqa: S603 - binary comes from PATH probe
            [binary, "gateway", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"healthy": False, "error": str(exc)}
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {"healthy": False, "error": (completed.stderr or "").strip()}
    health = payload.get("health")
    rpc = payload.get("rpc")
    return {
        "healthy": bool(isinstance(health, dict) and health.get("healthy")),
        "rpc_ok": bool(isinstance(rpc, dict) and rpc.get("ok")),
        "port": (payload.get("gateway") or {}).get("port")
        if isinstance(payload.get("gateway"), dict)
        else None,
        "state": ((payload.get("service") or {}).get("runtime") or {}).get("state")
        if isinstance(payload.get("service"), dict)
        else None,
    }
