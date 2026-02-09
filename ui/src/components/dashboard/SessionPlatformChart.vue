<template>
  <div class="rounded-xl border border-stone-800/70 bg-stone-950/40 p-4">
    <h4 class="text-sm font-medium text-stone-300 mb-4">By Platform</h4>
    <div class="space-y-3">
      <div
        v-for="platform in platforms"
        :key="platform.name"
        class="flex items-center justify-between"
      >
        <span class="text-sm text-stone-400 capitalize">{{ platform.label }}</span>
        <span class="text-sm font-medium text-stone-200">
          {{ data[platform.name] || 0 }}
        </span>
      </div>
      <div v-if="total === 0" class="text-sm text-stone-500 text-center py-4">
        No sessions yet
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  data: Record<string, number>;
}>();

const platforms = [
  { name: "telegram", label: "Telegram" },
  { name: "slack", label: "Slack" },
  { name: "discord", label: "Discord" },
  { name: "none", label: "No platform" },
];

const total = computed(() => {
  return Object.values(props.data).reduce((sum, count) => sum + count, 0);
});
</script>
