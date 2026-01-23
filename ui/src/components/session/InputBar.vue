<script setup lang="ts">
import { computed, ref, watch } from "vue"
import { ArrowUp, ChevronUp, Square, FileCode } from "lucide-vue-next"
import type { SessionState } from "@/api"
import { Textarea } from "@/components/ui/textarea"

interface Props {
  sessionState: SessionState | null
  sending: boolean
  viewMode: "chat" | "diff"
  hasDiff: boolean
  hasGit: boolean
}

const props = defineProps<Props>()

const emit = defineEmits<{
  send: [text: string]
  stop: []
  preset: [text: string]
  "update:viewMode": [mode: "chat" | "diff"]
}>()

const prompt = ref("")
const presetsCollapsed = ref(true)

const basePresets = [
  { label: "Approve", text: "I approve this change." },
  { label: "Deny", text: "I reject this change." },
  { label: "Summarize", text: "Please summarize what you've done." },
  { label: "Retry", text: "Please try again." }
]

const gitPresets = [
  { label: "Staged", text: "List the currently staged files and summarize their changes." }
]

const presets = computed(() =>
  props.hasGit ? [...basePresets, ...gitPresets] : basePresets
)

const canStop = computed(() =>
  props.sessionState === "RUNNING" || props.sessionState === "AWAITING_INPUT"
)

const canSend = computed(() =>
  props.sessionState !== "INTERRUPTING" &&
  props.sessionState !== "ERROR"
)

const isSessionRunning = computed(() => props.sessionState === "RUNNING")

const hasPrompt = computed(() => Boolean(prompt.value.trim()))

const canShowPresets = computed(() =>
  props.sessionState === "RUNNING" || props.sessionState === "AWAITING_INPUT"
)

const handleSend = () => {
  if (prompt.value.trim() && canSend.value) {
    emit("send", prompt.value.trim())
    prompt.value = ""
  }
}

const handleStop = () => {
  if (canStop.value) {
    emit("stop")
  }
}

const sendPreset = (text: string) => {
  emit("preset", text)
}

const toggleDiff = () => {
  emit("update:viewMode", props.viewMode === "chat" ? "diff" : "chat")
}
</script>

<template>
  <div class="fixed bottom-0 left-0 right-0 z-40 border-t border-stone-800/50 bg-stone-950/95 backdrop-blur">
    <!-- Quick replies (visible by default when session active) -->
    <transition name="slide">
      <div
        v-if="!presetsCollapsed && canShowPresets && viewMode === 'chat'"
        class="border-b border-stone-800/50 px-4 py-3"
      >
        <div class="mx-auto grid max-w-sm grid-cols-2 gap-2">
          <button
            v-for="preset in presets"
            :key="preset.label"
            class="flex h-11 items-center justify-center rounded-xl border border-stone-700 bg-stone-800/60 text-sm font-medium text-stone-200 transition active:scale-95 hover:bg-stone-700"
            :disabled="sending"
            @click="sendPreset(preset.text)"
          >
            {{ preset.label }}
          </button>
        </div>
        <button
          class="mx-auto mt-2 block text-xs text-stone-500 transition hover:text-stone-300"
          @click="presetsCollapsed = true"
        >
          Hide shortcuts
        </button>
      </div>
    </transition>

    <div class="mx-auto max-w-3xl px-4 py-3">
      <div v-if="viewMode === 'chat'" class="flex items-end gap-2">
        <!-- Presets toggle (only shows when collapsed) -->
        <button
          v-if="canShowPresets && presetsCollapsed"
          class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
          @click="presetsCollapsed = false"
          title="Show shortcuts"
        >
          <ChevronUp class="h-5 w-5" />
        </button>

        <!-- Input area -->
        <div class="relative flex-1">
          <Textarea
            v-model="prompt"
            rows="1"
            class="max-h-32 min-h-[44px] w-full resize-none rounded-2xl border-stone-700 bg-stone-900 py-3 pl-4 pr-12 text-sm text-stone-100 placeholder-stone-500 focus:border-stone-600 focus:ring-0"
            placeholder="Message"
            @keydown.enter.exact.prevent="handleSend"
            @keydown.enter.shift.exact.stop
          />
          <!-- Send/Stop button -->
          <button
            v-if="isSessionRunning && !sending"
            class="absolute bottom-2 right-2 flex h-8 w-8 items-center justify-center rounded-full bg-stone-600 text-white transition hover:bg-stone-500"
            @click="handleStop"
            title="Stop"
          >
            <Square class="h-3.5 w-3.5" fill="currentColor" />
          </button>
          <button
            v-else
            class="absolute bottom-2 right-2 flex h-8 w-8 items-center justify-center rounded-full transition"
            :class="hasPrompt && canSend
              ? 'bg-emerald-600 text-white hover:bg-emerald-500'
              : 'bg-stone-700 text-stone-400'"
            :disabled="!hasPrompt || !canSend || sending"
            @click="handleSend"
            title="Send"
          >
            <ArrowUp class="h-4 w-4" />
          </button>
        </div>

        <!-- Diff toggle -->
        <button
          v-if="hasGit || hasDiff"
          class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
          @click="toggleDiff"
          title="View diff"
        >
          <FileCode class="h-5 w-5" />
        </button>
      </div>

      <!-- Diff mode indicator -->
      <div v-else class="flex items-center justify-between">
        <span class="text-sm text-stone-400">Viewing changes</span>
        <button
          class="rounded-full bg-stone-800 px-4 py-2 text-sm text-stone-200 transition hover:bg-stone-700"
          @click="toggleDiff"
        >
          Back to chat
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.slide-enter-active,
.slide-leave-active {
  transition: all 0.2s ease;
}
.slide-enter-from,
.slide-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>
