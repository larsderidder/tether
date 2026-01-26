<template>
  <header class="sticky top-0 z-20 border-b border-stone-800/40 bg-stone-950/95 backdrop-blur">
    <div class="mx-auto flex h-14 w-full max-w-3xl items-center justify-between px-4">
      <!-- Left: menu + title -->
      <div class="flex min-w-0 flex-1 items-center gap-3">
        <button
          class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-stone-300 transition hover:bg-stone-800"
          @click="$emit('openDrawer')"
        >
          <Menu class="h-5 w-5" />
        </button>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <img src="/logo.png" alt="Tether" class="h-5" />
            <span class="text-sm font-medium text-stone-100">Tether</span>
            <span
              v-if="statusDot"
              class="h-2 w-2 shrink-0 rounded-full"
              :class="statusDot"
              :title="activeSession?.state"
            ></span>
          </div>
          <p v-if="activeSession" class="truncate text-xs text-stone-500">
            {{ activeSession.name || activeSession.directory || 'New session' }}
          </p>
        </div>
      </div>

      <!-- Right: actions -->
      <div class="flex items-center gap-1">
        <!-- Sync button -->
        <button
          v-if="hasActiveSession"
          class="flex h-10 w-10 items-center justify-center rounded-lg text-stone-400 transition hover:bg-stone-800 hover:text-stone-200 disabled:opacity-50"
          :disabled="syncing"
          @click="$emit('sync')"
          title="Sync messages from CLI"
        >
          <RefreshCw class="h-5 w-5" :class="{ 'animate-spin': syncing }" />
        </button>

        <div class="relative" ref="menuRef">
          <button
            v-if="hasActiveSession"
            class="flex h-10 w-10 items-center justify-center rounded-lg text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
            @click="menuOpen = !menuOpen"
            title="Options"
          >
            <MoreVertical class="h-5 w-5" />
          </button>

          <!-- Dropdown menu -->
          <transition name="fade">
            <div
              v-if="menuOpen"
              class="absolute right-0 top-full mt-1 w-40 rounded-xl border border-stone-800 bg-stone-900 py-1 shadow-xl"
            >
              <button
                class="w-full px-3 py-2 text-left text-sm text-stone-300 transition hover:bg-stone-800"
                @click="handleRename"
              >
                Rename
              </button>
              <button
                class="w-full px-3 py-2 text-left text-sm text-stone-300 transition hover:bg-stone-800"
                @click="handleInfo"
              >
                Session info
              </button>
            </div>
          </transition>
        </div>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { Menu, MoreVertical, RefreshCw } from "lucide-vue-next";
import type { Session } from "@/api";

const props = defineProps<{
  activeSession: Session | undefined;
  hasActiveSession: boolean;
  syncing: boolean;
  statusDot: string;
}>();

const emit = defineEmits<{
  openDrawer: [];
  sync: [];
  rename: [];
  info: [];
}>();

const menuOpen = ref(false);
const menuRef = ref<HTMLElement | null>(null);

const handleRename = () => {
  emit("rename");
  menuOpen.value = false;
};

const handleInfo = () => {
  emit("info");
  menuOpen.value = false;
};

const handleClickOutside = (event: MouseEvent | TouchEvent) => {
  if (!menuOpen.value) return;
  if (menuRef.value?.contains(event.target as Node)) return;
  menuOpen.value = false;
};

onMounted(() => {
  window.addEventListener("click", handleClickOutside);
  window.addEventListener("touchstart", handleClickOutside);
});

onUnmounted(() => {
  window.removeEventListener("click", handleClickOutside);
  window.removeEventListener("touchstart", handleClickOutside);
});
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
