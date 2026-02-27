<template>
  <transition name="fade">
    <div
      v-if="visible"
      class="fixed inset-0 z-50 flex items-center justify-center bg-stone-950/90 px-4"
    >
      <div class="w-full max-w-sm rounded-2xl border border-stone-800 bg-stone-900 p-6">
        <div class="mb-6 text-center">
          <h2 class="text-xl font-semibold text-stone-100">Welcome to Tether</h2>
          <p class="mt-1 text-sm text-stone-400">Enter a directory to get started</p>
        </div>
        <div class="space-y-3">
          <button
            class="flex w-full items-center justify-center gap-2 rounded-lg bg-emerald-600 py-2.5 text-sm font-medium text-white transition hover:bg-emerald-500"
            @click="$emit('attach')"
          >
            <Link class="h-4 w-4" />
            Attach to existing session
          </button>
          <div class="relative py-2">
            <div class="absolute inset-0 flex items-center">
              <div class="w-full border-t border-stone-700"></div>
            </div>
            <div class="relative flex justify-center text-xs">
              <span class="bg-stone-900 px-2 text-stone-500">or start new</span>
            </div>
          </div>
          <DirectoryInput
            :model-value="directoryInput"
            @update:model-value="$emit('update:directoryInput', $event)"
            :checking="checking"
            :probe="probe"
          />
          <button
            class="w-full rounded-lg border border-stone-700 bg-stone-800/50 py-2.5 text-sm font-medium text-stone-300 transition hover:bg-stone-800 disabled:opacity-50"
            @click="$emit('create')"
            :disabled="!probe?.exists || checking || creating"
          >
            {{ checking ? 'Checking...' : 'Start session' }}
          </button>
          <p v-if="probe && !probe.exists" class="text-center text-xs text-rose-400">
            Directory not found
          </p>
          <p v-else-if="error" class="text-center text-xs text-rose-400">{{ error }}</p>
        </div>
      </div>
    </div>
  </transition>
</template>

<script setup lang="ts">
import { Link } from "lucide-vue-next";
import DirectoryInput from "./DirectoryInput.vue";
import type { DirectoryCheck } from "@/api";

defineProps<{
  visible: boolean;
  directoryInput: string;
  checking: boolean;
  probe: DirectoryCheck | null;
  error: string;
  creating: boolean;
}>();

defineEmits<{
  "update:directoryInput": [value: string];
  attach: [];
  create: [];
}>();
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
