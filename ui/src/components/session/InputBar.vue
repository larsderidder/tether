<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from "vue"
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
const bottomOffset = ref(0)

// Handle visual viewport changes for mobile keyboard
const updateBottomOffset = () => {
  if (!window.visualViewport) {
    bottomOffset.value = 0
    return
  }
  // Calculate offset when keyboard is open
  const viewportHeight = window.visualViewport.height
  const windowHeight = window.innerHeight
  const offset = windowHeight - viewportHeight - window.visualViewport.offsetTop
  bottomOffset.value = Math.max(0, offset)
}

const handleFocus = () => {
  // Small delay to let the keyboard appear
  setTimeout(updateBottomOffset, 100)
}

const handleBlur = () => {
  bottomOffset.value = 0
}

onMounted(() => {
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", updateBottomOffset)
  }
})

onUnmounted(() => {
  if (window.visualViewport) {
    window.visualViewport.removeEventListener("resize", updateBottomOffset)
  }
})

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
  props.sessionState === "RUNNING" ||
  props.sessionState === "AWAITING_INPUT" ||
  props.sessionState === "ERROR"
)

const canSend = computed(() =>
  props.sessionState !== "INTERRUPTING"
)

const isSessionRunning = computed(() => props.sessionState === "RUNNING")

const hasPrompt = computed(() => Boolean(prompt.value.trim()))

const canShowPresets = computed(() =>
  props.sessionState === "RUNNING" ||
  props.sessionState === "AWAITING_INPUT" ||
  props.sessionState === "ERROR"
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
  <Teleport to="#input-bar-slot">
    <div class="input-bar-container" :style="{ paddingBottom: `calc(env(safe-area-inset-bottom) + ${bottomOffset}px)` }">
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
          class="mb-[2px] flex size-11 shrink-0 items-center justify-center rounded-full bg-stone-800/60 text-stone-400 transition hover:bg-stone-700 hover:text-stone-200 active:scale-95"
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
            class="max-h-20 min-h-[44px] w-full resize-none rounded-2xl border-stone-700 bg-stone-900 py-3 pl-4 pr-12 text-sm text-stone-100 placeholder-stone-500 focus:border-stone-600 focus:ring-0 overscroll-contain"
            placeholder="Message"
            @keydown.enter.exact.prevent="handleSend"
            @keydown.enter.shift.exact.stop
            @focus="handleFocus"
            @blur="handleBlur"
          />
          <!-- Send/Stop button -->
          <button
            v-if="isSessionRunning && !sending"
            class="send-button absolute bottom-2 right-2 flex h-8 w-8 items-center justify-center rounded-full bg-stone-600 text-white transition hover:bg-stone-500 active:scale-95"
            @click="handleStop"
            title="Stop"
          >
            <Square class="h-3.5 w-3.5" fill="currentColor" />
          </button>
          <button
            v-else
            class="send-button absolute bottom-2 right-2 flex h-8 w-8 items-center justify-center rounded-full transition active:scale-95"
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
          class="mb-[2px] flex size-11 shrink-0 items-center justify-center rounded-full bg-stone-800/60 text-stone-400 transition hover:bg-stone-700 hover:text-stone-200 active:scale-95"
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
          class="rounded-full bg-stone-800 px-4 py-2 text-sm text-stone-200 transition hover:bg-stone-700 active:scale-95 active:bg-stone-600"
          @click="toggleDiff"
        >
          Back to chat
        </button>
      </div>
    </div>
    </div>
  </Teleport>
</template>

<style scoped>
.input-bar-container {
  flex-shrink: 0;
  border-top: 1px solid rgb(41 37 36 / 0.5);
  background-color: rgb(12 10 9);
}

/* Eliminate 300ms tap delay on mobile for all buttons */
.input-bar-container button {
  touch-action: manipulation;
  -webkit-user-select: none;
  user-select: none;
}

/* Specific styling for send button touch target */
.send-button {
  touch-action: manipulation;
  -webkit-user-select: none;
  user-select: none;
  cursor: pointer;
}

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
