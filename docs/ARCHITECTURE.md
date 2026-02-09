# Architecture

Visual overview of Tether's structure. See the linked docs for details.

## System Overview

```mermaid
graph TB
    subgraph External Agents
        EA1[Claude Code]
        EA2[Custom Agent]
    end

    subgraph Agent Adapters
        CS[Claude Subprocess]
        CA[Claude API]
        LR[LiteLLM]
        CX[Codex Sidecar]
    end

    subgraph Tether Agent
        API[REST API]
        MCP[MCP Server]
        SE[Session Engine]
        Store[(Session Store)]
        SSE[SSE Stream]
        BS[Bridge Subscriber]
    end

    subgraph Consumers
        VUE[Web UI — Vue PWA]
        TG[Telegram]
        SL[Slack]
        DC[Discord]
    end

    EA1 & EA2 -->|MCP tools| MCP
    EA1 & EA2 -->|REST / WebSocket| API
    MCP --> API

    CS & CA & LR & CX -->|RunnerEvents callbacks| SE
    API --> SE
    SE --> Store
    Store -->|all events, unfiltered| SSE --> VUE
    Store -->|subscriber queue| BS -->|filtered: final output, approvals, state| TG & SL & DC

    VUE -->|REST calls| API
    TG & SL & DC -->|REST calls| API
```

Runners and external agents produce events. The session engine persists them to the store,
which broadcasts to subscriber queues. Two independent consumers read those queues — the
SSE stream (raw passthrough for the web UI) and bridge subscribers (filtered, server-side
rendering for messaging platforms). All consumers send input back through the same REST API.

See: [Runners](RUNNERS.md) · [Session Engine](SESSION_ENGINE.md) · [Bridges](BRIDGES.md) · [Web UI](WEB_UI.md) · [MCP Server](MCP_SERVER.md)

## Event Flow

How a single event travels from a runner to all consumers.

```mermaid
flowchart LR
    R[Runner] -->|callback| ARE[ApiRunnerEvents]
    ARE -->|emit_output etc.| E[store.emit]
    E --> LOG[(JSONL log)]
    E --> Q1[SSE queue]
    E --> Q2[Bridge queue]

    Q1 -->|all event types\nincluding intermediate| SSE[SSE endpoint]
    SSE -->|EventSource| VUE[Web UI]

    Q2 --> BS[BridgeSubscriber._consume]
    BS -->|"final=True output\npermission_request\nsession_state changes"| BM[Bridge methods]
    BM --> Platform[Telegram / Slack / Discord]

    style LOG fill:#f5f5f5,stroke:#999
```

The store appends every event to the JSONL log and pushes it to every subscriber queue.
SSE forwards everything — the Vue app decides what to render. The bridge subscriber
filters aggressively: only final output, permission requests, and state transitions
reach the messaging platform.

See: [Session Engine > Event Distribution](SESSION_ENGINE.md#event-distribution)

## Session Lifecycle

```mermaid
stateDiagram-v2
    [*] --> CREATED
    CREATED --> RUNNING : POST /start
    RUNNING --> AWAITING_INPUT : turn complete
    RUNNING --> INTERRUPTING : POST /interrupt
    RUNNING --> ERROR : runner error
    AWAITING_INPUT --> RUNNING : POST /input or /start
    INTERRUPTING --> AWAITING_INPUT : graceful stop
    INTERRUPTING --> ERROR : error during interrupt
    ERROR --> RUNNING : POST /start (restart)
```

Transitions are enforced by `api/state.py` with per-session async locks.
Invalid transitions return HTTP 409.

See: [Data Model](DATA_MODEL.md) · [Session Engine](SESSION_ENGINE.md)

## Interaction Loop

How user input and approval responses flow back to the runner.

```mermaid
sequenceDiagram
    participant User as User (Web UI / Bridge)
    participant API as REST API
    participant Store as Session Store
    participant Runner as Runner

    Note over User,Runner: User Input
    User->>API: POST /sessions/{id}/input
    API->>Store: transition → RUNNING
    API->>Runner: send_input(text)
    Runner->>Store: emit(output events...)
    Store-->>User: SSE stream / bridge notification

    Note over User,Runner: Permission Request
    Runner->>Store: emit(permission_request)
    Store-->>User: SSE stream / bridge notification
    User->>API: POST /sessions/{id}/permission
    API->>Store: resolve_pending_permission()
    Store-->>Runner: asyncio.Future resolved
    Runner->>Store: emit(permission_resolved)
```

Bridges add an auto-approve layer: `check_auto_approve()` in the bridge base class
can resolve permissions without user interaction when allow-all or allow-tool timers
are active.

See: [API Reference](API_REFERENCE.md) · [Bridges > Auto-Approve](BRIDGES.md#auto-approve-system)
