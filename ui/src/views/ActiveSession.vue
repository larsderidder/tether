<template>
  <section class="space-y-4">

    <Card v-if="infoOpen" class="border-stone-800/70 bg-stone-900/70">
      <CardHeader class="flex flex-col gap-3 p-4">
        <div class="flex items-center justify-between">
          <CardTitle class="text-sm uppercase tracking-[0.3em] text-stone-400">Session info</CardTitle>
          <Button size="sm" variant="ghost" @click="infoOpen = false">Close</Button>
        </div>
        <div class="flex flex-wrap gap-3 text-[11px] uppercase tracking-[0.2em] text-stone-400">
          <div
            v-if="session?.runner_type"
            class="rounded-xl border px-3 py-2"
            :class="runnerTypeStyles"
          >
            <p>Runner</p>
            <p class="mt-1 text-xs font-semibold capitalize">{{ session.runner_type }}</p>
          </div>
          <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
            <p>Directory</p>
            <p class="mt-1 max-w-[20ch] break-all text-xs font-semibold text-stone-50">
              {{ session?.directory || "Unavailable" }}
            </p>
          </div>
          <div
            v-if="session?.directory_has_git"
            class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2 text-emerald-300"
          >
            <p>Git</p>
            <p class="mt-1 text-xs font-semibold">Detected</p>
          </div>
        </div>
      </CardHeader>
      <CardContent class="space-y-4 p-4">
        <div v-if="headerInfo" class="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
            <p class="text-[10px] uppercase tracking-[0.2em] text-stone-400">Version</p>
            <p class="mt-1 text-xs font-semibold text-stone-50">{{ headerInfo.version }}</p>
          </div>
          <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
            <p class="text-[10px] uppercase tracking-[0.2em] text-stone-400">Model</p>
            <p class="mt-1 text-xs font-semibold text-stone-50">{{ headerInfo.model }}</p>
          </div>
          <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
            <p class="text-[10px] uppercase tracking-[0.2em] text-stone-400">Provider</p>
            <p class="mt-1 text-xs font-semibold text-stone-50">{{ headerInfo.provider }}</p>
          </div>
          <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
            <p class="text-[10px] uppercase tracking-[0.2em] text-stone-400">Approval</p>
            <p class="mt-1 text-xs font-semibold text-stone-50">{{ headerInfo.approval }}</p>
          </div>
          <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
            <p class="text-[10px] uppercase tracking-[0.2em] text-stone-400">Sandbox</p>
            <p class="mt-1 text-xs font-semibold text-stone-50">{{ headerInfo.sandbox }}</p>
          </div>
          <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
            <p class="text-[10px] uppercase tracking-[0.2em] text-stone-400">Session ID</p>
            <p class="mt-1 break-all font-mono text-xs font-semibold text-stone-50">
              {{ headerInfo.sessionId }}
            </p>
          </div>
        </div>
        <p v-else class="text-sm text-stone-500">No header yet.</p>
        <details
          v-if="session?.runner_header"
          class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2 text-stone-200"
        >
          <summary class="cursor-pointer text-xs font-semibold uppercase tracking-[0.2em] text-stone-400">
            Raw header
          </summary>
          <pre class="mt-2 whitespace-pre-wrap font-mono text-xs text-stone-300">
{{ session.runner_header }}
          </pre>
        </details>
      </CardContent>
    </Card>

    <Card v-if="viewMode === 'chat'" class="border-0 bg-transparent shadow-none">
      <CardContent class="space-y-4 p-4">
        <div class="min-h-[50vh] space-y-3">
          <p v-if="!messages.length" class="text-center text-sm text-stone-500">
            Start a session by sending a prompt.
          </p>
          <div
            v-for="(message, index) in messages"
            :key="index"
            class="max-w-[85%] rounded-2xl px-4 py-3 text-sm leading-relaxed"
            :class="message.role === 'user' ? 'ml-auto bg-stone-900/70 text-stone-50' : 'bg-stone-900/70 text-stone-50'"
          >
            <p v-if="message.role === 'user'">{{ message.text }}</p>
            <div v-else class="space-y-2">
              <div class="flex items-center justify-between text-[11px] uppercase tracking-[0.3em] text-stone-400">
                <span>Agent</span>
                <div class="flex items-center gap-2">
                  <button
                    class="rounded-full bg-stone-900/70 p-1 text-stone-300 transition hover:text-stone-100"
                    @click.stop="copyFinal(message)"
                    :disabled="!message.final"
                    :title="message.final ? 'Copy final answer' : 'No text to copy'"
                  >
                    <Copy class="h-3 w-3 text-stone-300" />
                  </button>
                  <button
                    class="rounded-full bg-stone-900/70 p-1 text-stone-300 transition hover:text-stone-100"
                    @click="message.showDetails = !message.showDetails"
                    title="Toggle details"
                  >
                    <Eye class="h-3 w-3" />
                  </button>
                </div>
              </div>
              <div v-if="message.thinking" class="flex items-start gap-2 text-sm text-stone-200">
                <span
                  v-if="assistantIndex === index && isSessionRunning"
                  class="inline-flex h-2 w-2 animate-pulse rounded-full bg-emerald-400"
                ></span>
                <span class="italic" v-html="renderMarkdown(message.thinking)"></span>
              </div>
              <p v-if="message.final" class="text-sm text-stone-100" v-html="renderMarkdown(message.final)"></p>
              <div
                v-if="message.showDetails"
                class="mt-2 rounded-2xl bg-stone-900/70 p-3 text-xs text-stone-200"
              >
                <div class="flex flex-wrap gap-2">
                  <button
                    class="rounded-full border px-3 py-1 text-[10px] uppercase tracking-[0.2em]"
                    :class="message.activeSection === 'header'
                      ? 'border-stone-400 bg-stone-800 text-stone-100'
                      : 'border-stone-600 text-stone-300'"
                    @click="message.activeSection = 'header'"
                  >
                    Header
                  </button>
                  <button
                    class="rounded-full border px-3 py-1 text-[10px] uppercase tracking-[0.2em]"
                    :class="message.activeSection === 'thinking'
                      ? 'border-stone-400 bg-stone-800 text-stone-100'
                      : 'border-stone-600 text-stone-300'"
                    @click="message.activeSection = 'thinking'"
                  >
                    Thinking
                  </button>
                  <button
                    class="rounded-full border px-3 py-1 text-[10px] uppercase tracking-[0.2em]"
                    :class="message.activeSection === 'final'
                      ? 'border-stone-400 bg-stone-800 text-stone-100'
                      : 'border-stone-600 text-stone-300'"
                    @click="message.activeSection = 'final'"
                  >
                    Final
                  </button>
                  <button
                    class="rounded-full border px-3 py-1 text-[10px] uppercase tracking-[0.2em]"
                    :class="message.activeSection === 'metadata'
                      ? 'border-stone-400 bg-stone-800 text-stone-100'
                      : 'border-stone-600 text-stone-300'"
                    @click="message.activeSection = 'metadata'"
                  >
                    Metadata
                  </button>
                </div>
                <div class="mt-2 rounded-lg bg-stone-950/50 p-2 text-stone-200">
                  <div
                    v-if="message.activeSection === 'header'"
                    class="message-markdown"
                    v-html="renderMarkdown(message.header || 'No header')"
                  ></div>
                  <div
                    v-else-if="message.activeSection === 'thinking'"
                    class="message-markdown"
                    v-html="renderMarkdown(message.thinking || 'No thinking')"
                  ></div>
                  <div
                    v-else-if="message.activeSection === 'final'"
                    class="message-markdown"
                    v-html="renderMarkdown(message.final || 'No final')"
                  ></div>
                  <div
                    v-else
                    class="message-markdown"
                    v-html="renderMarkdown(message.metadata || 'No metadata')"
                  ></div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>

    <Card v-else class="border-0 bg-transparent shadow-none">
      <CardHeader class="flex flex-row items-center justify-between space-y-0 p-4">
        <CardTitle class="text-sm uppercase tracking-[0.3em] text-stone-400">Changes</CardTitle>
        <Button variant="outline" size="sm" @click="copyDiff" :disabled="!diff">
          Copy all
        </Button>
      </CardHeader>
      <CardContent class="space-y-3 p-4">
        <p v-if="!diffFileList.length" class="text-sm text-stone-500">No changes yet.</p>
        <details
          v-for="file in diffFileList"
          :key="file.id"
          class="rounded-2xl border border-stone-700/70 bg-stone-950/60"
        >
          <summary
            class="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-semibold text-stone-200 [&::-webkit-details-marker]:hidden"
          >
            <div>
              <p class="text-sm text-stone-50">{{ file.path }}</p>
              <p class="text-xs text-stone-400">{{ file.hunks }} hunks</p>
            </div>
            <Button variant="ghost" size="icon" @click.stop="copyFile(file.patch)" :disabled="!file.patch">
              <Copy class="h-3 w-3" />
            </Button>
          </summary>
          <div class="border-t border-stone-800 bg-stone-900/80 px-4 py-3">
            <div class="diff2html" v-html="file.html"></div>
          </div>
        </details>
      </CardContent>
    </Card>

    <div class="fixed bottom-4 left-4 right-4 z-40 rounded-2xl bg-stone-900/80 p-3 shadow-xl backdrop-blur">
      <form v-if="viewMode === 'chat'" class="flex items-center gap-2" @submit.prevent="handlePrimaryAction">
        <Textarea
          v-model="prompt"
          rows="1"
          class="min-h-[40px] flex-1 resize-none border-0 bg-stone-900/80 text-base text-stone-50 placeholder-stone-500 focus:ring-0"
          placeholder="Give instructions"
          @keydown.enter.exact.prevent="handlePrimaryAction"
          @keydown.enter.shift.exact.stop
        />
        <Button
          type="submit"
          :variant="primaryActionVariant"
          size="icon"
          class="self-center"
          :disabled="primaryActionDisabled"
          :title="primaryActionLabel"
        >
          <component :is="primaryActionIcon" class="h-4 w-4" />
        </Button>
      </form>
      <Tabs v-model="viewMode" class="mt-2 w-full">
        <TabsList class="flex w-full rounded-2xl bg-stone-950/60 p-1">
          <TabsTrigger
            class="flex-1 text-center data-[state=active]:bg-emerald-500/20 data-[state=active]:text-emerald-100"
            value="chat"
          >
            Chat
          </TabsTrigger>
          <TabsTrigger
            class="flex-1 text-center data-[state=active]:bg-emerald-500/20 data-[state=active]:text-emerald-100"
            value="diff"
            :disabled="!(session?.directory_has_git || diff)"
          >
            Diff
          </TabsTrigger>
        </TabsList>
      </Tabs>
    </div>

    <p v-if="error" class="text-sm text-rose-400">{{ error }}</p>

    <transition name="fade">
      <div
        v-if="renameOpen"
        class="fixed inset-0 z-50 flex items-center justify-center bg-stone-950/80 px-4"
      >
        <Card class="w-full max-w-sm space-y-3 border border-stone-800/80 bg-stone-900/80 p-4">
          <div class="flex items-center justify-between gap-4">
            <p class="text-sm font-semibold uppercase tracking-[0.3em] text-stone-400">Rename session</p>
            <Button variant="ghost" size="icon" @click="renameOpen = false">
              <X class="h-4 w-4" />
            </Button>
          </div>
          <div class="space-y-3">
            <Input v-model="renameValue" placeholder="Session name" />
            <div class="flex items-center justify-end gap-2">
              <Button variant="ghost" @click="renameOpen = false">Cancel</Button>
              <Button
                variant="secondary"
                @click="applyRename"
                :disabled="renaming || !renameValue.trim()"
              >
                Save
              </Button>
            </div>
            <p
              v-if="renameMessage"
              :class="renameMessage === 'Updated' ? 'text-emerald-400' : 'text-rose-400'"
              class="text-xs"
            >
              {{ renameMessage }}
            </p>
          </div>
        </Card>
      </div>
    </transition>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  createSession,
  getDirectoryDiff,
  getDiff,
  getSession,
  openEventStream,
  renameSession,
  sendInput,
  startSession,
  stopSessionKeepalive,
  stopSession,
  type DiffFile,
  type DiffResponse,
  type EventEnvelope,
  type Session
} from "../api";
import { activeSessionId, requestInfo, requestRename } from "../state";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import * as Diff2Html from "diff2html";
import { Copy, Eye, Send, StopCircle, X } from "lucide-vue-next";

const session = ref<Session | null>(null);
type ChatMessage = {
  role: "user" | "assistant";
  text?: string;
  header?: string;
  thinking?: string;
  final?: string;
  metadata?: string;
  showDetails?: boolean;
  activeSection?: "header" | "thinking" | "final" | "metadata";
};

const messages = ref<ChatMessage[]>([]);
const diff = ref("");
const diffFiles = ref<
  { id: string; path: string; hunks: number; html: string; patch: string }[]
>([]);
const error = ref("");
const prompt = ref("");
const sending = ref(false);
const lastSeq = ref(0);
const viewMode = ref<"chat" | "diff">("chat");
const infoOpen = ref(false);
const renameOpen = ref(false);
const renameValue = ref("");
const renaming = ref(false);
const renameMessage = ref("");
let closeStream: (() => void) | null = null;
let assistantIndex = -1;
let reconnectTimer: number | null = null;

const canStop = computed(() => session.value?.state === "RUNNING");
const canSend = computed(
  () => session.value?.state === "CREATED" || session.value?.state === "RUNNING"
);
const isSessionRunning = computed(() => session.value?.state === "RUNNING");
const primaryActionIsStop = computed(() => isSessionRunning.value && !sending.value);
const primaryActionLabel = computed(() => {
  if (primaryActionIsStop.value) {
    return "Stop";
  }
  if (sending.value) {
    return "Sending…";
  }
  return "Send";
});
const primaryActionVariant = computed(() =>
  primaryActionIsStop.value ? "destructive" : "secondary"
);
const primaryActionDisabled = computed(() => {
  if (primaryActionIsStop.value) {
    return !canStop.value;
  }
  const hasPrompt = Boolean(prompt.value.trim());
  return sending.value || !canSend.value || !hasPrompt;
});
const primaryActionIcon = computed(() => (primaryActionIsStop.value ? StopCircle : Send));

const reportError = (err: unknown, target: "error" | "rename" = "error") => {
  const message = String(err);
  if (
    message.includes("input stream") ||
    message.includes("Failed to fetch") ||
    message.includes("NetworkError")
  ) {
    console.error(err);
    return;
  }
  if (target === "rename") {
    renameMessage.value = message;
  } else {
    error.value = message;
  }
};

const handlePrimaryAction = async () => {
  if (primaryActionIsStop.value) {
    await stop();
    return;
  }
  await start();
};
const headerInfo = computed(() => parseRunnerHeader(session.value?.runner_header || ""));

const runnerTypeStyles = computed(() => {
  const type = session.value?.runner_type;
  if (type === "claude") {
    return "border-amber-500/60 bg-amber-900/30 text-amber-300";
  }
  // Default to codex styling
  return "border-emerald-500/60 bg-emerald-900/30 text-emerald-300";
});

const buildDiffView = (diffText: string, files: DiffFile[]) => {
  const parsedFiles = Diff2Html.parse(diffText);
  const htmlByPath = new Map<string, string>();
  parsedFiles.forEach((file) => {
    const path = (file.newName || file.oldName || "unknown").replace(/^b\//, "");
    const html = Diff2Html.html([file], {
      inputFormat: "json",
      showFiles: false,
      matching: "lines",
      outputFormat: "line-by-line"
    });
    htmlByPath.set(path, html);
  });
  return files.map((file, index) => ({
    id: `${index}-${file.path}`,
    path: file.path,
    hunks: file.hunks,
    patch: file.patch,
    html:
      htmlByPath.get(file.path) ||
      Diff2Html.html(file.patch, {
        inputFormat: "diff",
        showFiles: false,
        matching: "lines",
        outputFormat: "line-by-line"
      })
  }));
};

const diffFileList = computed(() =>
  Array.isArray(diffFiles.value) ? diffFiles.value : []
);

const resetView = () => {
  messages.value = [];
  diff.value = "";
  error.value = "";
  prompt.value = "";
  assistantIndex = -1;
  renameOpen.value = false;
  renameMessage.value = "";
};

const ensureSession = async () => {
  error.value = "";
  if (!activeSessionId.value) {
    return;
  }
  try {
    session.value = await getSession(activeSessionId.value);
  } catch (err) {
    reportError(err);
  }
};

const openStream = async () => {
  if (!activeSessionId.value) {
    return;
  }
  if (closeStream) {
    closeStream();
    closeStream = null;
  }
  try {
    closeStream = await openEventStream(activeSessionId.value, onEvent, onError);
  } catch (err) {
    reportError(err);
  }
};

const resolveDiffResponse = async (): Promise<DiffResponse> => {
  if (!activeSessionId.value) {
    return { diff: "", files: [] };
  }
  const directoryPath = session.value?.directory;
  if (directoryPath) {
    return getDirectoryDiff(directoryPath);
  }
  return getDiff(activeSessionId.value);
};

const refreshDiff = async () => {
  error.value = "";
  if (!activeSessionId.value) {
    return;
  }
  try {
    const fetched = await resolveDiffResponse();
    const diffText = fetched.diff || "";
    const files = Array.isArray(fetched.files) ? fetched.files : parseRawDiff(diffText);
    diff.value = diffText;
    const rendered = buildDiffView(diffText, files);
    diffFiles.value = Array.isArray(rendered) ? rendered : [];
  } catch (err) {
    reportError(err);
  }
};

const start = async () => {
  error.value = "";
  if (!activeSessionId.value) {
    return;
  }
  const value = prompt.value.trim();
  if (!value) {
    error.value = "Prompt required.";
    return;
  }
  sending.value = true;
  messages.value.push({ role: "user", text: value });
  messages.value.push({
    role: "assistant",
    header: session.value?.runner_header || "",
    thinking: "",
    final: "",
    metadata: "",
    showDetails: false,
    activeSection: "final"
  });
  assistantIndex = messages.value.length - 1;
  prompt.value = "";
  try {
    if (session.value?.state === "RUNNING") {
      session.value = await sendInput(activeSessionId.value, value);
    } else {
      session.value = await startSession(activeSessionId.value, value);
    }
  } catch (err) {
    reportError(err);
  } finally {
    sending.value = false;
  }
};

const stop = async () => {
  error.value = "";
  if (!activeSessionId.value) {
    return;
  }
  try {
    session.value = await stopSession(activeSessionId.value);
  } catch (err) {
    reportError(err);
  }
};

const onEvent = (event: EventEnvelope) => {
  const seq = Number((event as { seq?: number }).seq || 0);
  if (seq && seq <= lastSeq.value) {
    return;
  }
  if (seq) {
    lastSeq.value = seq;
  }
  if (event.type === "output") {
    const payload = event.data as { text?: string; kind?: string };
    const text = String(payload.text || "");
    const kind = payload.kind || "final";
    if (assistantIndex < 0 || !messages.value[assistantIndex]) {
      messages.value.push({
        role: "assistant",
        header: session.value?.runner_header || "",
        thinking: "",
        final: "",
        metadata: "",
        showDetails: false,
        activeSection: "final"
      });
      assistantIndex = messages.value.length - 1;
    }
    const message = messages.value[assistantIndex];
    if (message.role !== "assistant") {
      return;
    }
    if (kind === "step") {
      message.thinking = `${message.thinking || ""}${text}`;
    } else {
      message.final = `${message.final || ""}${text}`;
    }
  }
  if (event.type === "metadata") {
    const payload = event.data as { raw?: string; key?: string; value?: unknown };
    const raw = payload.raw || "";
    const rendered = raw
      ? `${raw}\n`
      : `${payload.key || "meta"}: ${JSON.stringify(payload.value)}\n`;
    if (assistantIndex >= 0 && messages.value[assistantIndex]?.role === "assistant") {
      const message = messages.value[assistantIndex];
      message.metadata = `${message.metadata || ""}${rendered}`;
    }
  }
  if (event.type === "heartbeat") {
    const payload = event.data as { elapsed_s?: number; done?: boolean };
    if (assistantIndex >= 0 && messages.value[assistantIndex]?.role === "assistant") {
      const message = messages.value[assistantIndex];
      const elapsed = Number(payload.elapsed_s || 0).toFixed(1);
      const status = payload.done ? "done" : "running";
      message.metadata = `${message.metadata || ""}heartbeat: ${elapsed}s (${status})\n`;
    }
    if (payload.done && session.value) {
      session.value.state = "STOPPED";
    }
  }
  if (event.type === "session_state") {
    if (session.value) {
      session.value.state = String((event.data as { state?: string }).state || "");
    }
  }
};

const onError = (err: unknown) => {
  const message = String(err);
  if (
    message.includes("input stream") ||
    message.includes("Failed to fetch") ||
    message.includes("NetworkError")
  ) {
    console.error(err);
    scheduleReconnect();
    return;
  }
  error.value = message;
};

const scheduleReconnect = () => {
  if (reconnectTimer || !activeSessionId.value) {
    return;
  }
  if (closeStream) {
    closeStream();
    closeStream = null;
  }
  reconnectTimer = window.setTimeout(async () => {
    reconnectTimer = null;
    if (!activeSessionId.value) {
      return;
    }
    try {
      closeStream = await openEventStream(activeSessionId.value, onEvent, onError);
    } catch (err) {
      onError(err);
    }
  }, 1000);
};

const stopOnUnload = () => {
  if (!activeSessionId.value) {
    return;
  }
  if (session.value?.state !== "RUNNING") {
    return;
  }
  stopSessionKeepalive(activeSessionId.value);
};

const openRename = () => {
  if (!activeSessionId.value) {
    return;
  }
  renameOpen.value = true;
  renameValue.value = session.value?.name || session.value?.directory || "";
  renameMessage.value = "";
};

const openInfo = async () => {
  if (!activeSessionId.value) {
    return;
  }
  infoOpen.value = true;
  try {
    session.value = await getSession(activeSessionId.value);
    renameValue.value = session.value?.name || "";
  } catch (err) {
    reportError(err);
  }
};

const applyRename = async () => {
  if (!activeSessionId.value) {
    return;
  }
  if (!renameValue.value.trim()) {
    renameMessage.value = "Name cannot be empty.";
    return;
  }
  renaming.value = true;
  renameMessage.value = "";
  try {
    session.value = await renameSession(activeSessionId.value, renameValue.value);
    renameMessage.value = "Updated";
    renameOpen.value = false;
  } catch (err) {
    reportError(err, "rename");
  } finally {
    renaming.value = false;
  }
};

const copyDiff = async () => {
  if (!diff.value) {
    return;
  }
  try {
    await navigator.clipboard.writeText(diff.value);
  } catch (err) {
    reportError(err);
  }
};

const copyFile = async (patch: string) => {
  if (!patch) {
    return;
  }
  try {
    await navigator.clipboard.writeText(patch);
  } catch (err) {
    reportError(err);
  }
};

const copyFinal = async (message: ChatMessage) => {
  const text = message.final || "";
  if (!text) {
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    reportError(err);
  }
};

const parseRawDiff = (source: string): DiffFile[] => {
  const lines = source.split("\n");
  const files: { path: string; hunks: number; lines: string[] }[] = [];
  let current: { path: string; hunks: number; lines: string[] } | null = null;
  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      if (current) {
        files.push(current);
      }
      const match = line.match(/diff --git a\/(.+?) b\/(.+)/);
      const path = match ? match[2] : "unknown";
      current = { path, hunks: 0, lines: [line] };
      continue;
    }
    if (!current) {
      current = { path: "unknown", hunks: 0, lines: [] };
    }
    if (line.startsWith("@@")) {
      current.hunks += 1;
    }
    current.lines.push(line);
  }
  if (current) {
    files.push(current);
  }
  return files.map((file) => ({
    path: file.path,
    hunks: file.hunks,
    patch: file.lines.join("\n")
  }));
};

const parseRunnerHeader = (raw: string): {
  version: string;
  model: string;
  provider: string;
  approval: string;
  sandbox: string;
  sessionId: string;
} | null => {
  if (!raw) {
    return null;
  }
  const lines = raw
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && line !== "--------");
  const versionLine = lines[0] || "";
  const getValue = (key: string) => {
    const line = lines.find((item) => item.toLowerCase().startsWith(`${key}:`));
    return line ? line.split(":").slice(1).join(":").trim() : "unknown";
  };
  return {
    version: versionLine || "unknown",
    model: getValue("model"),
    provider: getValue("provider"),
    approval: getValue("approval"),
    sandbox: getValue("sandbox"),
    sessionId: getValue("session id")
  };
};

const renderMarkdown = (source: string): string => {
  const escaped = source
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  const lines = escaped.split("\n").map((line) => {
    const trimmed = line.trim();
    if (trimmed.toLowerCase() === "thinking") {
      return "<em>thinking</em>";
    }
    let out = line.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|\s)\*([^*]+)\*/g, "$1<em>$2</em>");
    return out;
  });
  return lines.join("<br />");
};

watch(viewMode, async (mode) => {
  if (mode === "diff") {
    await refreshDiff();
  }
});

watch(requestRename, () => {
  openRename();
});

watch(requestInfo, () => {
  openInfo();
});

watch(activeSessionId, async (newId, oldId) => {
  if (newId === oldId) {
    return;
  }
  resetView();
  renameOpen.value = false;
  infoOpen.value = false;
  renameMessage.value = "";
  session.value = null;
  if (closeStream) {
    closeStream();
    closeStream = null;
  }
  if (!newId) {
    return;
  }
  await ensureSession();
  await refreshDiff();
  await openStream();
});

onMounted(async () => {
  if (activeSessionId.value) {
    await ensureSession();
    await refreshDiff();
    await openStream();
  }
  window.addEventListener("beforeunload", stopOnUnload);
  window.addEventListener("pagehide", stopOnUnload);
});

onUnmounted(() => {
  if (reconnectTimer) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  window.removeEventListener("beforeunload", stopOnUnload);
  window.removeEventListener("pagehide", stopOnUnload);
  if (closeStream) {
    closeStream();
  }
});
</script>
