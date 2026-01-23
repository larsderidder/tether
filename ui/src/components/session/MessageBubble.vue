<script setup lang="ts">
import { computed } from "vue"
import { Bot, ChevronDown, ChevronUp, Copy, User } from "lucide-vue-next"

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

const renderMarkdown = (source: string): string => {
  const escaped = source
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
  const lines = escaped.split("\n").map((line) => {
    const trimmed = line.trim()
    if (trimmed.toLowerCase() === "thinking") {
      return "<em>thinking</em>"
    }
    let out = line.replace(/`([^`]+)`/g, "<code>$1</code>")
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    out = out.replace(/(^|\s)\*([^*]+)\*/g, "$1<em>$2</em>")
    return out
  })
  return lines.join("<br />")
}

const hasThinking = computed(() => Boolean(props.message.thinking?.trim()))
const hasFinal = computed(() => Boolean(props.message.final?.trim()))
const hasContent = computed(() => hasThinking.value || hasFinal.value)
</script>

<template>
  <!-- User message -->
  <div v-if="message.role === 'user'" class="flex justify-end gap-2">
    <div class="max-w-[80%] rounded-2xl rounded-br-md bg-emerald-600 px-4 py-2.5 text-sm text-white">
      {{ message.text }}
    </div>
    <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-600">
      <User class="h-4 w-4 text-white" />
    </div>
  </div>

  <!-- Assistant message -->
  <div v-else class="flex gap-2">
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
  </div>
</template>
