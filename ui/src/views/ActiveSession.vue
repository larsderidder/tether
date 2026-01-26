<template>
  <section class="space-y-4 pb-48">
    <SessionInfoPanel
      :session="session"
      :header="headerData"
      :open="infoOpen"
      @close="infoOpen = false"
    />

    <ChatMessageList
      v-if="viewMode === 'chat'"
      :messages="messages"
      :assistant-index="assistantIndex"
      :is-running="isSessionRunning"
      @copy-final="copyFinal"
      @toggle-details="toggleDetails"
    />

    <DiffViewer
      v-else
      :files="diffFileList"
      :diff="diff"
      @copy-all="copyDiff"
      @copy-file="copyFile"
    />

    <InputBar
      :session-state="session?.state ?? null"
      :sending="sending"
      :view-mode="viewMode"
      :has-diff="Boolean(diff)"
      :has-git="session?.directory_has_git ?? false"
      @send="handleSend"
      @stop="handleStop"
      @preset="handlePreset"
      @update:view-mode="viewMode = $event"
    />

    <p v-if="error" class="text-sm text-rose-400">{{ error }}</p>

    <Dialog :open="renameOpen" @update:open="renameOpen = $event">
      <DialogContent class="max-w-sm">
        <DialogHeader>
          <DialogTitle>Rename session</DialogTitle>
        </DialogHeader>
        <div class="space-y-3">
          <Input v-model="renameValue" placeholder="Session name" />
          <div class="flex items-center justify-end gap-2">
            <Button variant="ghost" class="h-10" @click="renameOpen = false">Cancel</Button>
            <Button
              variant="secondary"
              class="h-10"
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
      </DialogContent>
    </Dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue"
import {
  getDirectoryDiff,
  getDiff,
  getSession,
  openEventStream,
  renameSession,
  sendInput,
  startSession,
  interruptSessionKeepalive,
  interruptSession,
  type DiffFile,
  type DiffResponse,
  type EventEnvelope,
  type Session
} from "../api"
import { activeSessionId, requestInfo, requestRename } from "../state"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle
} from "@/components/ui/dialog"
import SessionInfoPanel from "@/components/session/SessionInfoPanel.vue"
import ChatMessageList from "@/components/session/ChatMessageList.vue"
import DiffViewer from "@/components/session/DiffViewer.vue"
import InputBar from "@/components/session/InputBar.vue"
import type { ChatMessage } from "@/components/session/MessageBubble.vue"
import * as Diff2Html from "diff2html"
import { useClipboard } from "@/composables/useClipboard"

const { copy } = useClipboard({ legacy: true })

const session = ref<Session | null>(null)
const messages = ref<ChatMessage[]>([])
const pendingUserInput = ref<string | null>(null) // Track optimistic user message
const headerData = ref<{
  title: string
  model?: string
  provider?: string
  sandbox?: string
  approval?: string
  session_id?: string
} | null>(null)
const diff = ref("")
const diffFiles = ref<
  { id: string; path: string; hunks: number; html: string; patch: string }[]
>([])
const error = ref("")
const sending = ref(false)
const lastSeq = ref(0)
const viewMode = ref<"chat" | "diff">("chat")
const infoOpen = ref(false)
const renameOpen = ref(false)
const renameValue = ref("")
const renaming = ref(false)
const renameMessage = ref("")

let closeStream: (() => void) | null = null
const assistantIndex = ref(-1)
let reconnectTimer: number | null = null
let reconnectAttempts = 0
const MAX_RECONNECT_ATTEMPTS = 10
const BASE_RECONNECT_DELAY_MS = 1000
const MAX_RECONNECT_DELAY_MS = 30000

const isSessionRunning = computed(() => session.value?.state === "RUNNING")

const diffFileList = computed(() =>
  Array.isArray(diffFiles.value) ? diffFiles.value : []
)

const reportError = (err: unknown, target: "error" | "rename" = "error") => {
  const message = String(err)
  if (
    message.includes("input stream") ||
    message.includes("Failed to fetch") ||
    message.includes("NetworkError")
  ) {
    console.error(err)
    return
  }
  if (target === "rename") {
    renameMessage.value = message
  } else {
    error.value = message
  }
}

const buildDiffView = (diffText: string, files: DiffFile[]) => {
  const parsedFiles = Diff2Html.parse(diffText)
  const htmlByPath = new Map<string, string>()
  parsedFiles.forEach((file) => {
    const path = (file.newName || file.oldName || "unknown").replace(/^b\//, "")
    const html = Diff2Html.html([file], {
      inputFormat: "json",
      showFiles: false,
      matching: "lines",
      outputFormat: "line-by-line"
    })
    htmlByPath.set(path, html)
  })
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
  }))
}

const resetView = () => {
  messages.value = []
  diff.value = ""
  error.value = ""
  assistantIndex.value = -1
  lastSeq.value = 0
  renameOpen.value = false
  renameMessage.value = ""
}

const ensureSession = async (): Promise<boolean> => {
  error.value = ""
  if (!activeSessionId.value) return false
  try {
    session.value = await getSession(activeSessionId.value)
    return true
  } catch (err) {
    // If session doesn't exist (404), clear the active session ID
    if (String(err).includes("404")) {
      activeSessionId.value = null
      return false
    }
    reportError(err)
    return false
  }
}

const openStream = async () => {
  if (!activeSessionId.value) return
  if (closeStream) {
    closeStream()
    closeStream = null
  }
  reconnectAttempts = 0
  try {
    closeStream = await openEventStream(activeSessionId.value, onEvent, onError, {
      since: lastSeq.value
    })
  } catch (err) {
    reportError(err)
  }
}

const resolveDiffResponse = async (): Promise<DiffResponse> => {
  if (!activeSessionId.value) return { diff: "", files: [] }
  const directoryPath = session.value?.directory
  if (directoryPath) return getDirectoryDiff(directoryPath)
  return getDiff(activeSessionId.value)
}

const refreshDiff = async () => {
  error.value = ""
  if (!activeSessionId.value) return
  try {
    const fetched = await resolveDiffResponse()
    const diffText = fetched.diff || ""
    const files = Array.isArray(fetched.files) ? fetched.files : parseRawDiff(diffText)
    diff.value = diffText
    const rendered = buildDiffView(diffText, files)
    diffFiles.value = Array.isArray(rendered) ? rendered : []
  } catch (err) {
    reportError(err)
  }
}

const start = async (value: string) => {
  error.value = ""
  if (!activeSessionId.value) return
  if (!value) {
    error.value = "Prompt required."
    return
  }
  // Ensure session data is loaded before sending (prevents race after session switch)
  if (!session.value) {
    await ensureSession()
  }
  sending.value = true
  pendingUserInput.value = value // Mark as pending to avoid duplicate from event
  messages.value.push({ role: "user", text: value })
  messages.value.push({
    role: "assistant",
    header: session.value?.runner_header || "",
    thinking: "",
    final: "",
    metadata: "",
    showDetails: false,
    activeSection: "final"
  })
  assistantIndex.value = messages.value.length - 1
  try {
    if (session.value?.state === "RUNNING" || session.value?.state === "AWAITING_INPUT") {
      session.value = await sendInput(activeSessionId.value, value)
    } else {
      try {
        session.value = await startSession(activeSessionId.value, value)
      } catch (startErr) {
        if (String(startErr).includes("409")) {
          session.value = await sendInput(activeSessionId.value, value)
        } else {
          throw startErr
        }
      }
    }
  } catch (err) {
    reportError(err)
  } finally {
    sending.value = false
  }
}

const interrupt = async () => {
  error.value = ""
  if (!activeSessionId.value) return
  try {
    session.value = await interruptSession(activeSessionId.value)
  } catch (err) {
    reportError(err)
  }
}

const handleSend = (text: string) => start(text)
const handleStop = () => interrupt()
const handlePreset = (text: string) => start(text)

const toggleDetails = (index: number) => {
  const msg = messages.value[index]
  if (msg) msg.showDetails = !msg.showDetails
}

const onEvent = (event: EventEnvelope) => {
  // Ignore events for other sessions (can happen during session switch race)
  if (event.session_id !== activeSessionId.value) {
    return
  }
  const seq = Number((event as { seq?: number }).seq || 0)
  if (seq && seq <= lastSeq.value) return
  if (seq) lastSeq.value = seq

  if (event.type === "output") {
    const payload = event.data as { text?: string; kind?: string }
    const text = String(payload.text || "")
    const kind = payload.kind || "final"
    if (assistantIndex.value < 0 || !messages.value[assistantIndex.value]) {
      messages.value.push({
        role: "assistant",
        header: session.value?.runner_header || "",
        thinking: "",
        final: "",
        metadata: "",
        showDetails: false,
        activeSection: "final"
      })
      assistantIndex.value = messages.value.length - 1
    }
    const message = messages.value[assistantIndex.value]
    if (message.role !== "assistant") return
    if (kind === "step") {
      const existing = message.thinking || ""
      message.thinking = existing ? `${existing}\n\n${text}` : text
    } else {
      const existing = message.final || ""
      message.final = existing ? `${existing}\n\n${text}` : text
    }
  }

  if (event.type === "metadata") {
    const payload = event.data as { raw?: string; key?: string; value?: unknown }
    const raw = payload.raw || ""
    const rendered = raw
      ? `${raw}\n`
      : `${payload.key || "meta"}: ${JSON.stringify(payload.value)}\n`
    if (assistantIndex.value >= 0 && messages.value[assistantIndex.value]?.role === "assistant") {
      const message = messages.value[assistantIndex.value]
      message.metadata = `${message.metadata || ""}${rendered}`
    }
  }

  if (event.type === "heartbeat") {
    const payload = event.data as { elapsed_s?: number; done?: boolean }
    if (assistantIndex.value >= 0 && messages.value[assistantIndex.value]?.role === "assistant") {
      const message = messages.value[assistantIndex.value]
      const elapsed = Number(payload.elapsed_s || 0).toFixed(1)
      const status = payload.done ? "done" : "running"
      message.metadata = `${message.metadata || ""}heartbeat: ${elapsed}s (${status})\n`
    }
  }

  if (event.type === "user_input") {
    const text = String((event.data as { text?: string }).text || "")
    // Skip if this message was already added locally (optimistic update)
    if (pendingUserInput.value === text) {
      pendingUserInput.value = null // Clear pending, already added
      // Don't reset assistantIndex - we already created the assistant message in start()
    } else {
      // Replay or different message - add it and reset for new assistant message
      messages.value.push({ role: "user", text })
      assistantIndex.value = -1
    }
  }

  if (event.type === "header") {
    // Store structured header data directly
    const payload = event.data as {
      title?: string
      model?: string
      provider?: string
      sandbox?: string
      approval?: string
      session_id?: string
    }
    headerData.value = {
      title: payload.title || "Unknown",
      model: payload.model,
      provider: payload.provider,
      sandbox: payload.sandbox,
      approval: payload.approval,
      session_id: payload.session_id,
    }
  }

  if (event.type === "session_state") {
    if (session.value) {
      session.value.state = String((event.data as { state?: string }).state || "")
    }
  }
}

const onError = (err: unknown) => {
  const message = String(err)
  if (
    message.includes("input stream") ||
    message.includes("Failed to fetch") ||
    message.includes("NetworkError") ||
    message.includes("Stream closed")
  ) {
    console.error(err)
    scheduleReconnect()
    return
  }
  error.value = message
}

const scheduleReconnect = () => {
  if (reconnectTimer || !activeSessionId.value) return
  if (closeStream) {
    closeStream()
    closeStream = null
  }
  reconnectAttempts++
  if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
    error.value = `Connection lost after ${MAX_RECONNECT_ATTEMPTS} attempts. Please refresh the page.`
    return
  }
  const exponentialDelay = BASE_RECONNECT_DELAY_MS * Math.pow(1.5, reconnectAttempts - 1)
  const jitter = Math.random() * 500
  const delay = Math.min(exponentialDelay + jitter, MAX_RECONNECT_DELAY_MS)
  console.log(`Reconnecting in ${Math.round(delay)}ms (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`)
  reconnectTimer = window.setTimeout(async () => {
    reconnectTimer = null
    if (!activeSessionId.value) return
    try {
      closeStream = await openEventStream(activeSessionId.value, onEvent, onError)
      reconnectAttempts = 0
    } catch (err) {
      onError(err)
    }
  }, delay)
}

const interruptOnUnload = () => {
  if (!activeSessionId.value) return
  if (session.value?.state !== "RUNNING") return
  interruptSessionKeepalive(activeSessionId.value)
}

const openRename = () => {
  if (!activeSessionId.value) return
  renameOpen.value = true
  renameValue.value = session.value?.name || session.value?.directory || ""
  renameMessage.value = ""
}

const openInfo = async () => {
  if (!activeSessionId.value) return
  infoOpen.value = true
  try {
    session.value = await getSession(activeSessionId.value)
    renameValue.value = session.value?.name || ""
  } catch (err) {
    reportError(err)
  }
}

const applyRename = async () => {
  if (!activeSessionId.value) return
  if (!renameValue.value.trim()) {
    renameMessage.value = "Name cannot be empty."
    return
  }
  renaming.value = true
  renameMessage.value = ""
  try {
    session.value = await renameSession(activeSessionId.value, renameValue.value)
    renameMessage.value = "Updated"
    renameOpen.value = false
  } catch (err) {
    reportError(err, "rename")
  } finally {
    renaming.value = false
  }
}

const copyDiff = () => {
  if (diff.value) copy(diff.value)
}

const copyFile = (patch: string) => {
  if (patch) copy(patch)
}

const copyFinal = (message: ChatMessage) => {
  const text = message.final || ""
  if (text) copy(text)
}

const parseRawDiff = (source: string): DiffFile[] => {
  const lines = source.split("\n")
  const files: { path: string; hunks: number; lines: string[] }[] = []
  let current: { path: string; hunks: number; lines: string[] } | null = null
  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      if (current) files.push(current)
      const match = line.match(/diff --git a\/(.+?) b\/(.+)/)
      const path = match ? match[2] : "unknown"
      current = { path, hunks: 0, lines: [line] }
      continue
    }
    if (!current) current = { path: "unknown", hunks: 0, lines: [] }
    if (line.startsWith("@@")) current.hunks += 1
    current.lines.push(line)
  }
  if (current) files.push(current)
  return files.map((file) => ({
    path: file.path,
    hunks: file.hunks,
    patch: file.lines.join("\n")
  }))
}

watch(viewMode, async (mode) => {
  if (mode === "diff") await refreshDiff()
})

watch(requestRename, () => openRename())
watch(requestInfo, () => openInfo())

watch(activeSessionId, async (newId, oldId) => {
  if (newId === oldId) return
  resetView()
  renameOpen.value = false
  infoOpen.value = false
  renameMessage.value = ""
  session.value = null
  if (closeStream) {
    closeStream()
    closeStream = null
  }
  if (!newId) return
  const exists = await ensureSession()
  if (!exists) return
  await refreshDiff()
  await openStream()
})

onMounted(async () => {
  if (activeSessionId.value) {
    const exists = await ensureSession()
    if (exists) {
      await refreshDiff()
      await openStream()
    }
  }
  window.addEventListener("beforeunload", interruptOnUnload)
  window.addEventListener("pagehide", interruptOnUnload)
})

onUnmounted(() => {
  if (reconnectTimer) {
    window.clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  window.removeEventListener("beforeunload", interruptOnUnload)
  window.removeEventListener("pagehide", interruptOnUnload)
  if (closeStream) closeStream()
})
</script>
