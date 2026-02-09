<template>
  <div class="rounded-xl border border-stone-800/70 bg-stone-950/40 p-4">
    <h4 class="text-sm font-medium text-stone-300 mb-4">By State</h4>
    <div class="space-y-3">
      <div
        v-for="state in states"
        :key="state.name"
        class="flex items-center justify-between"
      >
        <div class="flex items-center gap-2">
          <div
            :class="[
              'w-2 h-2 rounded-full',
              state.color
            ]"
          />
          <span class="text-sm text-stone-400">{{ state.label }}</span>
        </div>
        <span class="text-sm font-medium text-stone-200">
          {{ data[state.name] || 0 }}
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

const states = [
  { name: "CREATED", label: "Created", color: "bg-stone-600" },
  { name: "RUNNING", label: "Running", color: "bg-blue-500" },
  { name: "AWAITING_INPUT", label: "Awaiting Input", color: "bg-amber-500" },
  { name: "INTERRUPTING", label: "Interrupting", color: "bg-orange-500" },
  { name: "ERROR", label: "Error", color: "bg-red-500" },
];

const total = computed(() => {
  return Object.values(props.data).reduce((sum, count) => sum + count, 0);
});
</script>
