<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from "vue"
import { Bot, ChevronDown, ChevronUp, Copy, User } from "lucide-vue-next"
import { renderMarkdown, decodeHtmlEntities } from "@/lib/markdown"
import { useClipboard } from "@/composables/useClipboard"

const { copy } = useClipboard({ legacy: true })

export type ChatMessage = {
  role: "user" | "assistant"
  text?: string
  header?: string
  thinking?: string
  final?: string
  metadata?: string
  showDetails?: boolean
  activeSection?: "header" | "thinking" | "final" | "metadata"
}

interface Props {
  message: ChatMessage
  isCurrentAssistant: boolean
  isRunning: boolean
}

const props = defineProps<Props>()

const emit = defineEmits<{
  copyFinal: []
  toggleDetails: []
}>()

const hasThinking = computed(() => Boolean(props.message.thinking?.trim()))
const hasFinal = computed(() => Boolean(props.message.final?.trim()))
const hasContent = computed(() => hasThinking.value || hasFinal.value)

// Selection copy functionality
const bubbleRef = ref<HTMLElement | null>(null)
const showSelectionCopy = ref(false)
const selectionCopyPos = ref({ x: 0, y: 0 })
const selectedText = ref("")

function handleSelectionChange() {
  const selection = window.getSelection()
  if (!selection || selection.isCollapsed || !bubbleRef.value) {
    showSelectionCopy.value = false
    return
  }

  const text = selection.toString().trim()
  if (!text) {
    showSelectionCopy.value = false
    return
  }

  // Check if selection is within this bubble
  const range = selection.getRangeAt(0)
  const container = range.commonAncestorContainer
  if (!bubbleRef.value.contains(container)) {
    showSelectionCopy.value = false
    return
  }

  selectedText.value = text
  const rect = range.getBoundingClientRect()
  const bubbleRect = bubbleRef.value.getBoundingClientRect()

  // Position above the selection, centered
  selectionCopyPos.value = {
    x: rect.left + rect.width / 2 - bubbleRect.left,
    y: rect.top - bubbleRect.top - 8
  }
  showSelectionCopy.value = true
}

function copySelection() {
  if (selectedText.value) {
    copy(selectedText.value)
    showSelectionCopy.value = false
    window.getSelection()?.removeAllRanges()
  }
}

function handleMouseDown(e: MouseEvent) {
  // Hide if clicking outside the copy button
  const target = e.target as HTMLElement
  if (!target.closest(".selection-copy-btn")) {
    showSelectionCopy.value = false
  }
}

onMounted(() => {
  document.addEventListener("selectionchange", handleSelectionChange)
  document.addEventListener("mousedown", handleMouseDown)
})

onUnmounted(() => {
  document.removeEventListener("selectionchange", handleSelectionChange)
  document.removeEventListener("mousedown", handleMouseDown)
})
</script>

<template>
  <!-- User message -->
  <div v-if="message.role === 'user'" ref="bubbleRef" class="relative flex justify-end gap-2">
    <div class="max-w-[80%] rounded-2xl rounded-br-md bg-emerald-600 px-4 py-2.5 text-sm text-white whitespace-pre-wrap">
      {{ decodeHtmlEntities(message.text || '') }}
    </div>
    <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-600">
      <User class="h-4 w-4 text-white" />
    </div>
    <!-- Selection copy button -->
    <button
      v-if="showSelectionCopy"
      class="selection-copy-btn absolute z-50 flex items-center gap-1 rounded-lg bg-stone-700 px-2 py-1 text-xs text-stone-200 shadow-lg transition hover:bg-stone-600"
      :style="{ left: selectionCopyPos.x + 'px', top: selectionCopyPos.y + 'px', transform: 'translate(-50%, -100%)' }"
      @mousedown.prevent
      @click="copySelection"
    >
      <Copy class="h-3 w-3" />
      <span>Copy</span>
    </button>
  </div>

  <!-- Assistant message -->
  <div v-else ref="bubbleRef" class="relative flex gap-2">
    <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-stone-700">
      <Bot class="h-4 w-4 text-stone-300" />
    </div>
    <div class="min-w-0 flex-1 space-y-2">
      <!-- Thinking preview (when no final yet) -->
      <div v-if="hasThinking && !hasFinal" class="space-y-1">
        <p class="message-thinking-markdown text-sm italic leading-relaxed text-stone-400" v-html="renderMarkdown(message.thinking!)"></p>
      </div>

      <!-- Main response -->
      <div v-if="hasFinal">
        <p
          class="message-markdown text-sm leading-relaxed text-stone-100"
          v-html="renderMarkdown(message.final!)"
        ></p>
      </div>

      <!-- Loading indicator (while running) -->
      <div
        v-if="isCurrentAssistant && isRunning"
        class="flex items-center gap-2"
      >
        <span class="flex gap-1">
          <span class="h-2 w-2 animate-bounce rounded-full bg-emerald-500" style="animation-delay: 0ms"></span>
          <span class="h-2 w-2 animate-bounce rounded-full bg-emerald-500" style="animation-delay: 150ms"></span>
          <span class="h-2 w-2 animate-bounce rounded-full bg-emerald-500" style="animation-delay: 300ms"></span>
        </span>
      </div>

      <!-- Actions row (only when done) -->
      <div v-if="hasFinal && !(isCurrentAssistant && isRunning)" class="flex items-center gap-1">
        <button
          class="flex h-7 w-7 items-center justify-center rounded-lg text-stone-500 transition hover:bg-stone-800 hover:text-stone-300"
          @click.stop="emit('copyFinal')"
          title="Copy response"
        >
          <Copy class="h-3.5 w-3.5" />
        </button>
        <button
          v-if="hasThinking"
          class="flex h-7 items-center gap-1 rounded-lg px-2 text-xs text-stone-500 transition hover:bg-stone-800 hover:text-stone-300"
          @click="emit('toggleDetails')"
        >
          <component :is="message.showDetails ? ChevronUp : ChevronDown" class="h-3.5 w-3.5" />
          <span>{{ message.showDetails ? 'Hide thinking' : 'Show thinking' }}</span>
        </button>
      </div>

      <!-- Expanded thinking panel -->
      <div
        v-if="message.showDetails && hasThinking"
        class="rounded-xl border border-stone-800 bg-stone-900/50 p-3"
      >
        <p class="mb-2 text-xs font-medium text-stone-500">Thinking</p>
        <div class="max-h-64 overflow-y-auto text-sm leading-relaxed text-stone-400">
          <p class="message-thinking-markdown italic" v-html="renderMarkdown(message.thinking!)"></p>
        </div>
      </div>
    </div>
    <!-- Selection copy button -->
    <button
      v-if="showSelectionCopy"
      class="selection-copy-btn absolute z-50 flex items-center gap-1 rounded-lg bg-stone-700 px-2 py-1 text-xs text-stone-200 shadow-lg transition hover:bg-stone-600"
      :style="{ left: selectionCopyPos.x + 'px', top: selectionCopyPos.y + 'px', transform: 'translate(-50%, -100%)' }"
      @mousedown.prevent
      @click="copySelection"
    >
      <Copy class="h-3 w-3" />
      <span>Copy</span>
    </button>
  </div>
</template>
