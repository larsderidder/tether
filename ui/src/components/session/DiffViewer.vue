<script setup lang="ts">
import { Copy } from "lucide-vue-next"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

export interface DiffFileDisplay {
  id: string
  path: string
  hunks: number
  html: string
  patch: string
}

interface Props {
  files: DiffFileDisplay[]
  diff: string
}

defineProps<Props>()

const emit = defineEmits<{
  copyAll: []
  copyFile: [patch: string]
}>()
</script>

<template>
  <Card class="border-0 bg-transparent shadow-none">
    <CardHeader class="flex flex-row items-center justify-between space-y-0 p-2 sm:p-4">
      <CardTitle class="text-xs sm:text-sm uppercase tracking-[0.2em] sm:tracking-[0.3em] text-stone-400">Changes</CardTitle>
      <Button variant="outline" size="sm" class="h-9 sm:h-10 text-xs sm:text-sm" @click="emit('copyAll')" :disabled="!diff">
        Copy all
      </Button>
    </CardHeader>
    <CardContent class="space-y-2 sm:space-y-3 p-2 sm:p-4">
      <p v-if="!files.length" class="text-sm text-stone-500">No changes yet.</p>
      <details
        v-for="file in files"
        :key="file.id"
        class="rounded-xl sm:rounded-2xl border border-stone-700/70 bg-stone-950/60"
      >
        <summary
          class="flex cursor-pointer list-none items-center justify-between gap-2 sm:gap-3 px-3 sm:px-4 py-2.5 sm:py-3 text-sm font-semibold text-stone-200 [&::-webkit-details-marker]:hidden"
        >
          <div class="min-w-0 flex-1">
            <p class="text-xs sm:text-sm text-stone-50 truncate">{{ file.path }}</p>
            <p class="text-[10px] sm:text-xs text-stone-400">{{ file.hunks }} hunks</p>
          </div>
          <Button
            variant="ghost"
            size="icon"
            class="h-9 w-9 sm:h-10 sm:w-10 shrink-0"
            @click.stop="emit('copyFile', file.patch)"
            :disabled="!file.patch"
          >
            <Copy class="h-4 w-4" />
          </Button>
        </summary>
        <div class="diff-content border-t border-stone-800 bg-stone-900/80 overflow-x-auto">
          <div class="diff2html min-w-0" v-html="file.html"></div>
        </div>
      </details>
    </CardContent>
  </Card>
</template>
