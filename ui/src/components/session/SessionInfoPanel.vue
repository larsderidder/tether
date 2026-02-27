<script setup lang="ts">
import type { Session } from "@/api"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

interface HeaderData {
  title: string
  model?: string
  provider?: string
  sandbox?: string
  approval?: string
  session_id?: string
}

interface Props {
  session: Session | null
  header: HeaderData | null
  open: boolean
}

const props = defineProps<Props>()
const emit = defineEmits<{ close: [] }>()

const runnerTypeStyles = () => {
  const type = props.session?.runner_type
  if (type === "claude") {
    return "border-amber-500/60 bg-amber-900/30 text-amber-300"
  }
  return "border-emerald-500/60 bg-emerald-900/30 text-emerald-300"
}
</script>

<template>
  <Card v-if="open" class="border-stone-800/70 bg-stone-900/70">
    <CardHeader class="flex flex-col gap-3 p-4">
      <div class="flex items-center justify-between">
        <CardTitle class="text-sm uppercase tracking-[0.3em] text-stone-400">Session info</CardTitle>
        <Button size="sm" variant="ghost" class="h-10 min-w-[60px]" @click="emit('close')">Close</Button>
      </div>
      <div class="flex flex-wrap gap-3 text-[11px] uppercase tracking-[0.2em] text-stone-400">
        <div
          v-if="session?.runner_type"
          class="rounded-xl border px-3 py-2"
          :class="runnerTypeStyles()"
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
      <div v-if="header" class="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
          <p class="text-xxs uppercase tracking-[0.2em] text-stone-400">Title</p>
          <p class="mt-1 text-xs font-semibold text-stone-50">{{ header.title }}</p>
        </div>
        <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
          <p class="text-xxs uppercase tracking-[0.2em] text-stone-400">Model</p>
          <p class="mt-1 text-xs font-semibold text-stone-50">{{ header.model || "unknown" }}</p>
        </div>
        <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
          <p class="text-xxs uppercase tracking-[0.2em] text-stone-400">Provider</p>
          <p class="mt-1 text-xs font-semibold text-stone-50">{{ header.provider || "unknown" }}</p>
        </div>
        <div v-if="header.approval" class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
          <p class="text-xxs uppercase tracking-[0.2em] text-stone-400">Approval</p>
          <p class="mt-1 text-xs font-semibold text-stone-50">{{ header.approval }}</p>
        </div>
        <div v-if="header.sandbox" class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
          <p class="text-xxs uppercase tracking-[0.2em] text-stone-400">Sandbox</p>
          <p class="mt-1 text-xs font-semibold text-stone-50">{{ header.sandbox }}</p>
        </div>
        <div class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
          <p class="text-xxs uppercase tracking-[0.2em] text-stone-400">Session ID</p>
          <p class="mt-1 break-all font-mono text-xs font-semibold text-stone-50">
            {{ header.session_id || session?.id || "unknown" }}
          </p>
        </div>
        <div v-if="session?.runner_session_id" class="rounded-xl border border-stone-700/80 bg-stone-900/40 px-3 py-2">
          <p class="text-xxs uppercase tracking-[0.2em] text-stone-400">Runner Session</p>
          <p class="mt-1 break-all font-mono text-xs font-semibold text-stone-50">
            {{ session.runner_session_id }}
          </p>
        </div>
      </div>
      <p v-else class="text-sm text-stone-500">No header yet.</p>
    </CardContent>
  </Card>
</template>
