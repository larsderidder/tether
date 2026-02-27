<template>
  <div class="rounded-xl border border-stone-800/70 bg-stone-950/40 p-4">
    <div class="flex items-center gap-3">
      <div class="flex-shrink-0">
        <div
          :class="[
            'w-3 h-3 rounded-full',
            statusColor
          ]"
        />
      </div>
      <div class="flex-1 min-w-0">
        <p class="text-sm font-medium text-stone-200 capitalize">{{ bridge.platform }}</p>
        <p class="text-xs text-stone-500 capitalize">{{ bridge.status.replace('_', ' ') }}</p>
      </div>
    </div>
    <div v-if="bridge.error_message" class="mt-2 text-xs text-red-400">
      {{ bridge.error_message }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { BridgeStatusInfo } from "@/api";

const props = defineProps<{
  bridge: BridgeStatusInfo;
}>();

const statusColor = computed(() => {
  switch (props.bridge.status) {
    case "running":
      return "bg-emerald-500";
    case "error":
      return "bg-red-500";
    case "not_configured":
      return "bg-stone-600";
    default:
      return "bg-stone-600";
  }
});
</script>
