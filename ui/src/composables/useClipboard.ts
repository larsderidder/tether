import { ref } from "vue"

export function useClipboard() {
  const error = ref<string | null>(null)
  const copied = ref(false)

  const copy = async (text: string): Promise<boolean> => {
    if (!text) {
      return false
    }
    error.value = null
    copied.value = false
    try {
      await navigator.clipboard.writeText(text)
      copied.value = true
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : "Failed to copy"
      return false
    }
  }

  return { copy, copied, error }
}
