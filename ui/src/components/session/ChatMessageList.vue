<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from "vue"
import MessageBubble, { type ChatMessage } from "./MessageBubble.vue"

interface Props {
  messages: ChatMessage[]
  assistantIndex: number
  isRunning: boolean
}

const props = defineProps<Props>()

const emit = defineEmits<{
  copyFinal: [message: ChatMessage]
  toggleDetails: [index: number]
}>()

const containerRef = ref<HTMLElement | null>(null)

const scrollToBottom = () => {
  nextTick(() => {
    if (!containerRef.value) return
    const messages = containerRef.value.querySelectorAll('[data-message]')
    const lastMessage = messages[messages.length - 1] as HTMLElement
    if (lastMessage) {
      const rect = lastMessage.getBoundingClientRect()
      const inputBarHeight = 180
      const targetY = window.scrollY + rect.bottom - window.innerHeight + inputBarHeight
      window.scrollTo({
        top: Math.max(0, targetY),
        behavior: "smooth"
      })
    }
  })
}

// Auto-scroll when messages change or content updates
watch(
  () => [
    props.messages.length,
    props.messages[props.assistantIndex]?.final?.length,
    props.messages[props.assistantIndex]?.thinking?.length
  ],
  () => scrollToBottom(),
  { deep: true }
)

onMounted(() => {
  if (props.messages.length) {
    scrollToBottom()
  }
})
</script>

<template>
  <div ref="containerRef" class="space-y-4 px-2">
    <div
      v-if="!messages.length"
      class="flex min-h-[40vh] items-center justify-center"
    >
      <div class="text-center">
        <p class="text-sm text-stone-500">Send a message to start</p>
      </div>
    </div>
    <div
      v-for="(message, index) in messages"
      :key="index"
      data-message
    >
      <MessageBubble
        :message="message"
        :is-current-assistant="assistantIndex === index"
        :is-running="isRunning"
        @copy-final="emit('copyFinal', message)"
        @toggle-details="emit('toggleDetails', index)"
      />
    </div>
  </div>
</template>
