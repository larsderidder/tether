<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h2>Session {{ sessionId }}</h2>
        <p v-if="session">State: {{ formatState(session.state) }}</p>
      </div>
      <div class="actions">
        <button @click="stop" :disabled="!session">Stop</button>
        <button @click="refreshDiff" :disabled="!session">Refresh diff</button>
      </div>
    </header>

    <section class="panel">
      <h3>Live Output</h3>
      <pre class="output">{{ output }}</pre>
      <div class="input-row">
        <input v-model="inputText" placeholder="Send a message…" />
        <button @click="send" :disabled="!session">Send</button>
      </div>
    </section>

    <section class="panel">
      <h3>Diff</h3>
      <pre class="diff">{{ diff }}</pre>
    </section>

    <p v-if="error" class="error">{{ error }}</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  getDiff,
  getSession,
  openEventStream,
  sendInput,
  startSession,
  stopSession,
  type EventEnvelope,
  type Session
} from "../api";

const route = useRoute();
const sessionId = String(route.params.id);
const session = ref<Session | null>(null);
const output = ref("");
const diff = ref("");
const error = ref("");
const inputText = ref("");
const lastSeq = ref(0);
let closeStream: (() => void) | null = null;

const formatState = (state: string | undefined): string => {
  if (!state) return "";
  const labels: Record<string, string> = {
    CREATED: "Ready",
    RUNNING: "Running",
    AWAITING_INPUT: "Awaiting input",
    INTERRUPTING: "Interrupting",
    ERROR: "Error"
  };
  return labels[state] || state.toLowerCase().replace(/_/g, " ");
};

const refresh = async () => {
  error.value = "";
  try {
    session.value = await getSession(sessionId);
    if (session.value.state === "CREATED") {
      session.value = await startSession(sessionId, "");
    }
  } catch (err) {
    error.value = String(err);
  }
};

const stop = async () => {
  error.value = "";
  try {
    session.value = await stopSession(sessionId);
  } catch (err) {
    error.value = String(err);
  }
};

const refreshDiff = async () => {
  error.value = "";
  try {
    const data = await getDiff(sessionId);
    diff.value = data.diff || "";
  } catch (err) {
    error.value = String(err);
  }
};

const send = async () => {
  const text = inputText.value.trim();
  if (!text) {
    return;
  }
  error.value = "";
  try {
    session.value = await sendInput(sessionId, text);
    inputText.value = "";
  } catch (err) {
    error.value = String(err);
  }
};

const onEvent = (event: EventEnvelope) => {
  const seq = Number((event as { seq?: number }).seq || 0);
  if (seq && seq <= lastSeq.value) return;
  if (seq) lastSeq.value = seq;
  if (event.type === "output") {
    const text = String((event.data as { text?: string }).text || "");
    output.value += text;
  }
  if (event.type === "user_input") {
    const text = String((event.data as { text?: string }).text || "");
    output.value += `\n> ${text}\n`;
  }
  if (event.type === "session_state") {
    if (session.value) {
      session.value.state = String((event.data as { state?: string }).state || "");
    }
  }
};

const onError = (err: unknown) => {
  error.value = String(err);
};

onMounted(async () => {
  await refresh();
  await refreshDiff();
  try {
    closeStream = await openEventStream(sessionId, onEvent, onError, { since: lastSeq.value });
  } catch (err) {
    error.value = String(err);
  }
});

onUnmounted(() => {
  if (closeStream) {
    closeStream();
  }
});
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-header {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.actions {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

button {
  padding: 10px 14px;
  border: none;
  background: var(--accent);
  color: #1b1c18;
  border-radius: 999px;
  font-weight: 600;
  width: 100%;
}

.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px;
  box-shadow: 0 12px 30px -26px rgba(0, 0, 0, 0.4);
}

.input-row {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.input-row input {
  flex: 1;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
  color: var(--ink);
}

.input-row button {
  width: auto;
}

.output,
.diff {
  margin: 0;
  white-space: pre-wrap;
  font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  font-size: 13px;
  color: var(--ink);
  max-height: 280px;
  overflow: auto;
}

.error {
  color: #9b1c1c;
}

@media (min-width: 720px) {
  .actions {
    flex-direction: row;
  }

  button {
    width: auto;
  }
}
</style>
