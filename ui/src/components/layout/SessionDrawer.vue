<template>
  <Sheet :open="open" @update:open="$emit('update:open', $event)">
    <SheetContent side="left" class="flex w-full max-w-[280px] flex-col border-stone-800/50 bg-stone-900 p-0 text-stone-50 [&>button]:hidden">
      <!-- Header with new session and attach buttons -->
      <div class="flex items-center justify-between border-b border-stone-800/50 px-4 py-3">
        <span class="text-sm font-medium text-stone-200">Tether</span>
        <div class="flex items-center gap-1">
          <button
            class="flex h-8 w-8 items-center justify-center rounded-lg text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
            @click="$emit('attach')"
            title="Attach to session"
          >
            <Link class="h-4 w-4" />
          </button>
          <button
            class="flex h-8 w-8 items-center justify-center rounded-lg text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
            @click="createPanelOpen = !createPanelOpen"
            title="New session"
          >
            <Plus class="h-5 w-5" />
          </button>
        </div>
      </div>

      <!-- Search and expand/collapse -->
      <div class="px-3 py-2">
        <div class="flex items-center gap-2">
          <Input
            v-model="searchQuery"
            placeholder="Search..."
            class="h-9 flex-1 rounded-lg border-stone-700 bg-stone-800/50 text-sm placeholder-stone-500"
          />
          <div v-if="groups.length > 1" class="flex gap-1">
            <button
              class="flex h-9 w-9 items-center justify-center rounded-lg bg-stone-800/50 text-stone-400 transition hover:bg-stone-800 hover:text-stone-300"
              @click="expandAll"
              title="Expand all"
            >
              <ChevronsUpDown class="h-4 w-4" />
            </button>
            <button
              class="flex h-9 w-9 items-center justify-center rounded-lg bg-stone-800/50 text-stone-400 transition hover:bg-stone-800 hover:text-stone-300"
              @click="collapseAll"
              title="Collapse all"
            >
              <ChevronsDownUp class="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>

      <!-- New session panel -->
      <transition name="fade">
        <div v-if="createPanelOpen" class="border-b border-stone-800/50 px-3 pb-3">
          <div class="space-y-2 rounded-lg bg-stone-800/50 p-3">
            <DirectoryInput
              v-model="directoryInput"
              :checking="checkingDirectory"
              :probe="directoryProbe"
              input-class="h-9 border-stone-700 bg-stone-900/50 pr-8 text-sm"
            />
            <button
              class="w-full rounded-lg bg-emerald-600 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-50"
              :disabled="checkingDirectory || !directoryInput.trim() || !directoryProbe?.exists"
              @click="handleCreate"
            >
              {{ checkingDirectory ? 'Checking...' : 'Start session' }}
            </button>
            <p v-if="directoryProbe && !directoryProbe.exists" class="text-center text-xs text-rose-400">
              Directory not found
            </p>
            <p v-else-if="directoryError" class="text-center text-xs text-rose-400">{{ directoryError }}</p>
          </div>
        </div>
      </transition>

      <!-- Sessions list -->
      <div class="flex-1 overflow-y-auto px-2 py-2">
        <div v-for="group in filteredGroups" :key="group.key" class="mb-2">
          <!-- Directory header (clickable to toggle) -->
          <div
            class="mb-0.5 flex w-full cursor-pointer items-center justify-between rounded-lg px-2 py-1.5 text-left transition hover:bg-stone-800/50"
            @click="toggle(group.key)"
          >
            <div class="flex min-w-0 flex-1 items-center gap-2">
              <ChevronRight
                class="h-3.5 w-3.5 shrink-0 text-stone-500 transition-transform duration-200"
                :class="{ 'rotate-90': isExpanded(group.key) }"
              />
              <Folder class="h-3.5 w-3.5 shrink-0 text-stone-500" />
              <span class="truncate text-xs font-medium text-stone-400">{{ group.label }}</span>
              <GitBranch v-if="group.hasGit" class="h-3 w-3 shrink-0 text-emerald-500" />
              <span class="shrink-0 text-[10px] text-stone-600">{{ group.sessions.length }}</span>
            </div>
            <button
              v-if="group.path"
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded text-stone-500 transition hover:bg-stone-700 hover:text-stone-300"
              @click.stop="$emit('addToDirectory', group.path)"
              :disabled="creating"
              title="Add session"
            >
              <Plus class="h-3.5 w-3.5" />
            </button>
          </div>

          <!-- Sessions in this directory (collapsible) -->
          <div v-if="isExpanded(group.key)" class="space-y-0.5 pl-5">
            <div
              v-for="session in group.sessions"
              :key="session.id"
              class="session-item group flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-left transition active:bg-stone-700"
              :class="session.id === activeSessionId
                ? 'bg-stone-800 text-stone-100'
                : 'text-stone-300 hover:bg-stone-800/50'"
              @click="$emit('select', session.id)"
            >
              <div class="min-w-0 flex-1">
                <p class="truncate text-sm text-stone-200">{{ session.name || 'New session' }}</p>
                <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-stone-500">
                  <span class="font-mono text-stone-600" :title="session.runner_session_id || session.id">{{ formatSessionId(session.runner_session_id || session.id) }}</span>
                  <span class="flex items-center gap-1">
                    <Clock class="h-3 w-3" />
                    {{ formatTime(session.last_activity_at) }}
                  </span>
                  <span class="flex items-center gap-1">
                    <MessageSquare class="h-3 w-3" />
                    {{ session.message_count }}
                  </span>
                  <span
                    v-if="session.state !== 'CREATED'"
                    class="flex items-center gap-1"
                    :class="getStateColor(session.state)"
                  >
                    <span
                      class="h-1.5 w-1.5 rounded-full"
                      :class="getStateDotClass(session.state)"
                    ></span>
                    {{ formatState(session.state) }}
                  </span>
                  <span
                    v-if="session.has_pending_permission"
                    class="flex items-center gap-1 text-amber-400"
                    title="Waiting for permission"
                  >
                    <ShieldAlert class="h-3 w-3 animate-pulse" />
                  </span>
                </div>
              </div>
              <button
                class="flex h-6 w-6 shrink-0 items-center justify-center rounded text-stone-500 opacity-0 transition hover:bg-stone-700 hover:text-rose-400 group-hover:opacity-100"
                @click.stop="$emit('delete', session.id)"
                :disabled="deleting"
                title="Delete"
              >
                <X class="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>

        <p v-if="!filteredGroups.length && searchQuery.trim()" class="px-2 text-center text-xs text-stone-500">
          No matches
        </p>
        <p v-else-if="!groups.length" class="px-2 text-center text-xs text-stone-500">
          No sessions yet
        </p>
      </div>

      <!-- Footer -->
      <div class="border-t border-stone-800/50 px-3 py-3">
        <button
          class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-sm text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
          @click="$emit('settings')"
        >
          <SettingsIcon class="h-4 w-4" />
          Settings
        </button>
      </div>
    </SheetContent>
  </Sheet>
</template>

<script setup lang="ts">
import { ref } from "vue";
import {
  Folder, GitBranch, Plus, Link, ChevronRight,
  Clock, MessageSquare, X, Settings as SettingsIcon,
  ChevronsDownUp, ChevronsUpDown, ShieldAlert
} from "lucide-vue-next";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { Input } from "@/components/ui/input";
import DirectoryInput from "./DirectoryInput.vue";
import { useSessionGroups, formatState, formatTime, formatSessionId } from "@/composables";
import type { Session, DirectoryCheck } from "@/api";
import type { ComputedRef } from "vue";

const props = defineProps<{
  open: boolean;
  sessions: ComputedRef<Session[]> | Session[];
  activeSessionId: string | null;
  creating: boolean;
  deleting: boolean;
  directoryInput: string;
  checkingDirectory: boolean;
  directoryProbe: DirectoryCheck | null;
  directoryError: string;
}>();

const emit = defineEmits<{
  "update:open": [value: boolean];
  "update:directoryInput": [value: string];
  select: [id: string];
  delete: [id: string];
  create: [];
  attach: [];
  addToDirectory: [path: string];
  settings: [];
}>();

// Use session groups composable
const sessionsRef = computed(() =>
  Array.isArray(props.sessions) ? props.sessions : props.sessions.value
);
const {
  searchQuery,
  groups,
  filteredGroups,
  isExpanded,
  toggle,
  expandAll,
  collapseAll
} = useSessionGroups(sessionsRef);

const createPanelOpen = ref(false);

// Proxy directoryInput to parent
import { computed, watch } from "vue";

const directoryInput = computed({
  get: () => props.directoryInput,
  set: (value) => emit("update:directoryInput", value)
});

const handleCreate = () => {
  emit("create");
  createPanelOpen.value = false;
};

const getStateColor = (state: string) => ({
  "text-emerald-400": state === "RUNNING",
  "text-amber-400": state === "AWAITING_INPUT",
  "text-orange-400": state === "INTERRUPTING",
  "text-rose-400": state === "ERROR"
});

const getStateDotClass = (state: string) => ({
  "bg-emerald-400": state === "RUNNING",
  "bg-amber-400": state === "AWAITING_INPUT",
  "bg-orange-400": state === "INTERRUPTING",
  "bg-rose-500": state === "ERROR",
  "animate-pulse": state === "RUNNING" || state === "AWAITING_INPUT"
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

/* Eliminate tap delay on all clickable elements */
button,
[role="button"],
.cursor-pointer,
.session-item {
  touch-action: manipulation;
  -webkit-user-select: none;
  user-select: none;
}
</style>
