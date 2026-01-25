<template>
  <Sheet :open="open" @update:open="$emit('update:open', $event)">
    <SheetContent side="right" class="flex w-full max-w-md flex-col border-stone-800/50 bg-stone-900 p-0 text-stone-50 [&>button]:hidden">
      <!-- Header -->
      <div class="flex items-center justify-between border-b border-stone-800/50 px-4 py-3">
        <div>
          <h2 class="text-sm font-medium text-stone-200">Attach to Session</h2>
          <p class="text-xs text-stone-500">Continue an existing session</p>
        </div>
        <button
          class="flex h-8 w-8 items-center justify-center rounded-lg text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
          @click="$emit('update:open', false)"
        >
          <X class="h-5 w-5" />
        </button>
      </div>

      <!-- Filters -->
      <div class="space-y-2 border-b border-stone-800/50 px-4 py-3">
        <Input
          v-model="directoryFilter"
          placeholder="Filter by directory..."
          class="h-9 rounded-lg border-stone-700 bg-stone-800/50 text-sm placeholder-stone-500"
        />
        <div class="flex gap-2">
          <button
            v-for="rt in runnerTypes"
            :key="rt.value"
            class="flex-1 rounded-lg px-3 py-1.5 text-xs font-medium transition"
            :class="runnerTypeFilter === rt.value
              ? 'bg-stone-700 text-stone-100'
              : 'bg-stone-800/50 text-stone-400 hover:bg-stone-800'"
            @click="runnerTypeFilter = rt.value"
          >
            {{ rt.label }}
          </button>
        </div>
      </div>

      <!-- Loading state -->
      <div v-if="loading" class="flex flex-1 items-center justify-center">
        <div class="text-center">
          <div class="mx-auto mb-2 h-6 w-6 animate-spin rounded-full border-2 border-stone-600 border-t-emerald-400"></div>
          <p class="text-sm text-stone-500">Loading sessions...</p>
        </div>
      </div>

      <!-- Error state -->
      <div v-else-if="loadError" class="flex flex-1 items-center justify-center px-4">
        <div class="text-center">
          <AlertCircle class="mx-auto mb-2 h-8 w-8 text-rose-400" />
          <p class="mb-3 text-sm text-stone-400">{{ loadError }}</p>
          <button
            class="rounded-lg bg-stone-800 px-4 py-2 text-sm text-stone-300 transition hover:bg-stone-700"
            @click="loadSessions"
          >
            Retry
          </button>
        </div>
      </div>

      <!-- Empty state -->
      <div v-else-if="!directoryGroups.length" class="flex flex-1 items-center justify-center px-4">
        <div class="text-center">
          <Folder class="mx-auto mb-2 h-8 w-8 text-stone-600" />
          <p class="text-sm text-stone-500">
            {{ sessions.length ? 'No sessions match filters' : 'No external sessions found' }}
          </p>
        </div>
      </div>

      <!-- Sessions list grouped by directory -->
      <div v-else class="flex-1 overflow-y-auto px-3 py-3">
        <div class="space-y-2">
          <div v-for="group in directoryGroups" :key="group.key">
            <!-- Directory group header -->
            <button
              class="mb-1 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition hover:bg-stone-800/50"
              @click="toggleDirectoryGroup(group.key)"
            >
              <ChevronRight
                class="h-3.5 w-3.5 shrink-0 text-stone-500 transition-transform duration-200"
                :class="{ 'rotate-90': isDirectoryGroupExpanded(group.key) }"
              />
              <Folder class="h-3.5 w-3.5 shrink-0 text-stone-500" />
              <span class="min-w-0 flex-1 truncate text-xs font-medium text-stone-400" :title="group.path">
                {{ group.label }}
              </span>
              <span class="shrink-0 text-[10px] text-stone-600">{{ group.sessions.length }}</span>
            </button>

            <!-- Sessions in this directory group -->
            <div v-if="isDirectoryGroupExpanded(group.key)" class="space-y-2 pl-5">
              <div
                v-for="session in group.sessions"
                :key="session.id"
                class="rounded-xl border border-stone-800/50 bg-stone-800/30 transition hover:bg-stone-800/50"
              >
                <!-- Session card header -->
                <button
                  class="w-full px-3 py-3 text-left"
                  @click="toggleSession(session)"
                >
                  <div class="flex items-start gap-3">
                    <!-- Runner type badge -->
                    <div
                      class="mt-0.5 flex h-6 shrink-0 items-center rounded-md px-2 text-xs font-medium"
                      :class="session.runner_type === 'claude_code'
                        ? 'bg-violet-500/20 text-violet-300'
                        : 'bg-blue-500/20 text-blue-300'"
                    >
                      {{ session.runner_type === 'claude_code' ? 'Claude' : 'Codex' }}
                    </div>

                    <div class="min-w-0 flex-1">
                      <!-- First prompt preview -->
                      <p class="line-clamp-2 text-sm text-stone-200">
                        {{ session.first_prompt || 'No prompt recorded' }}
                      </p>
                      <!-- Metadata row -->
                      <div class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-stone-500">
                        <span class="font-mono text-stone-600" :title="session.id">
                          {{ formatSessionId(session.id) }}
                        </span>
                        <span class="flex items-center gap-1">
                          <Clock class="h-3 w-3" />
                          {{ formatTime(session.last_activity) }}
                        </span>
                        <span class="flex items-center gap-1">
                          <MessageSquare class="h-3 w-3" />
                          {{ session.message_count }}
                        </span>
                        <span
                          v-if="session.is_running"
                          class="flex items-center gap-1 text-emerald-400"
                        >
                          <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400"></span>
                          Busy
                        </span>
                      </div>
                    </div>

                    <!-- Expand chevron -->
                    <ChevronDown
                      class="h-4 w-4 shrink-0 text-stone-500 transition"
                      :class="{ 'rotate-180': expandedSession === session.id }"
                    />
                  </div>
                </button>

                <!-- Expanded content -->
                <transition name="slide">
                  <div v-if="expandedSession === session.id" class="border-t border-stone-800/50">
                    <!-- History loading -->
                    <div v-if="loadingHistory" class="px-3 py-4">
                      <div class="flex items-center justify-center gap-2 text-sm text-stone-500">
                        <div class="h-4 w-4 animate-spin rounded-full border-2 border-stone-600 border-t-emerald-400"></div>
                        Loading history...
                      </div>
                    </div>

                    <!-- History -->
                    <div v-else-if="sessionDetail" class="max-h-64 overflow-y-auto px-3 py-2">
                      <div class="space-y-2">
                        <template v-for="(msg, i) in sessionDetail.messages" :key="i">
                          <div
                            v-if="msg.content?.trim()"
                            class="rounded-lg px-3 py-2 text-sm"
                            :class="msg.role === 'user'
                              ? 'bg-stone-700/50 text-stone-200'
                              : 'bg-stone-800/50 text-stone-300'"
                          >
                            <div class="mb-1 flex items-center gap-2">
                              <span class="text-xs font-medium" :class="msg.role === 'user' ? 'text-emerald-400' : 'text-violet-400'">
                                {{ msg.role === 'user' ? 'You' : 'Claude' }}
                              </span>
                              <span v-if="msg.timestamp" class="text-xs text-stone-500">
                                {{ formatTime(msg.timestamp) }}
                              </span>
                            </div>
                            <p class="line-clamp-4 whitespace-pre-wrap break-words text-stone-300">{{ msg.content }}</p>
                          </div>
                        </template>
                      </div>
                    </div>

                    <!-- Attach button -->
                    <div class="border-t border-stone-800/50 px-3 py-3">
                      <button
                        v-if="session.runner_type === 'claude_code'"
                        class="w-full rounded-lg py-2 text-sm font-medium transition"
                        :class="session.is_running
                          ? 'cursor-not-allowed bg-stone-700 text-stone-500'
                          : 'bg-emerald-600 text-white hover:bg-emerald-500'"
                        :disabled="attaching || session.is_running"
                        :title="session.is_running ? 'Cannot attach to busy session' : 'Attach to this session'"
                        @click="attachToSession(session)"
                      >
                        {{ attaching ? 'Attaching...' : session.is_running ? 'Session is busy' : 'Attach' }}
                      </button>
                      <div
                        v-else
                        class="rounded-lg bg-stone-800/50 px-3 py-2 text-center text-xs text-stone-500"
                      >
                        Codex CLI sessions are view-only
                      </div>
                    </div>
                  </div>
                </transition>
              </div>
            </div>
          </div>
        </div>
      </div>
    </SheetContent>
  </Sheet>
</template>

<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { Input } from "@/components/ui/input";
import {
  X,
  Folder,
  Clock,
  MessageSquare,
  ChevronDown,
  ChevronRight,
  AlertCircle,
} from "lucide-vue-next";
import {
  listExternalSessions,
  getExternalSessionHistory,
  attachToExternalSession,
  type ExternalSessionSummary,
  type ExternalSessionDetail,
  type ExternalRunnerType,
} from "@/api";

const props = defineProps<{
  open: boolean;
}>();

const emit = defineEmits<{
  (e: "update:open", value: boolean): void;
  (e: "attached", sessionId: string): void;
}>();

// State
const sessions = ref<ExternalSessionSummary[]>([]);
const loading = ref(false);
const loadError = ref("");
const directoryFilter = ref("");
const runnerTypeFilter = ref<ExternalRunnerType | "all">("all");
const expandedSession = ref<string | null>(null);
const sessionDetail = ref<ExternalSessionDetail | null>(null);
const loadingHistory = ref(false);
const attaching = ref(false);
const expandedDirectoryGroups = ref(new Set<string>());

const runnerTypes = [
  { value: "all" as const, label: "All" },
  { value: "claude_code" as const, label: "Claude" },
  { value: "codex_cli" as const, label: "Codex" },
];

// Computed
const filteredSessions = computed(() => {
  let result = sessions.value;

  if (runnerTypeFilter.value !== "all") {
    result = result.filter((s) => s.runner_type === runnerTypeFilter.value);
  }

  const filter = directoryFilter.value.trim().toLowerCase();
  if (filter) {
    result = result.filter(
      (s) =>
        s.directory.toLowerCase().includes(filter) ||
        (s.first_prompt?.toLowerCase().includes(filter) ?? false) ||
        s.id.toLowerCase().includes(filter)
    );
  }

  return result;
});

// Group filtered sessions by directory
type DirectoryGroup = {
  key: string;
  label: string;
  path: string;
  sessions: ExternalSessionSummary[];
};

const directoryGroups = computed((): DirectoryGroup[] => {
  const map = new Map<string, DirectoryGroup>();

  filteredSessions.value.forEach((session) => {
    const key = session.directory;
    if (!map.has(key)) {
      map.set(key, {
        key,
        label: formatDirectoryLabel(session.directory),
        path: session.directory,
        sessions: [],
      });
    }
    map.get(key)!.sessions.push(session);
  });

  // Sort groups by most recent activity
  return Array.from(map.values()).sort((a, b) => {
    const aLatest = Math.max(...a.sessions.map(s => new Date(s.last_activity).getTime()));
    const bLatest = Math.max(...b.sessions.map(s => new Date(s.last_activity).getTime()));
    return bLatest - aLatest;
  });
});

const isDirectoryGroupExpanded = (key: string) => {
  return expandedDirectoryGroups.value.has(key);
};

const toggleDirectoryGroup = (key: string) => {
  if (expandedDirectoryGroups.value.has(key)) {
    expandedDirectoryGroups.value.delete(key);
  } else {
    expandedDirectoryGroups.value.add(key);
  }
};

function formatDirectoryLabel(path: string): string {
  const segments = path.split("/").filter(Boolean);
  return segments.at(-1) || path;
}

function formatSessionId(id: string): string {
  // Show first 8 characters of session ID
  return id.slice(0, 8);
}

// Methods
async function loadSessions() {
  loading.value = true;
  loadError.value = "";
  try {
    sessions.value = await listExternalSessions({ limit: 50 });
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : "Failed to load sessions";
  } finally {
    loading.value = false;
  }
}

async function toggleSession(session: ExternalSessionSummary) {
  if (expandedSession.value === session.id) {
    expandedSession.value = null;
    sessionDetail.value = null;
    return;
  }

  expandedSession.value = session.id;
  sessionDetail.value = null;
  loadingHistory.value = true;

  try {
    sessionDetail.value = await getExternalSessionHistory(
      session.id,
      session.runner_type,
      20
    );
  } catch (err) {
    console.error("Failed to load session history:", err);
  } finally {
    loadingHistory.value = false;
  }
}

async function attachToSession(session: ExternalSessionSummary) {
  if (session.runner_type !== "claude_code" || session.is_running) {
    return;
  }

  attaching.value = true;
  try {
    const newSession = await attachToExternalSession(
      session.id,
      session.runner_type,
      session.directory
    );
    emit("attached", newSession.id);
    emit("update:open", false);
  } catch (err) {
    console.error("Failed to attach to session:", err);
    alert(err instanceof Error ? err.message : "Failed to attach to session");
  } finally {
    attaching.value = false;
  }
}

function formatDirectory(path: string): string {
  const segments = path.split("/").filter(Boolean);
  if (segments.length <= 2) {
    return path;
  }
  return `.../${segments.slice(-2).join("/")}`;
}

function formatTime(timestamp: string): string {
  const date = new Date(timestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

// Watch for open to load sessions
watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      loadSessions();
    } else {
      // Reset state when closed
      expandedSession.value = null;
      sessionDetail.value = null;
      directoryFilter.value = "";
      runnerTypeFilter.value = "all";
      expandedDirectoryGroups.value.clear();
    }
  }
);
</script>

<style scoped>
.slide-enter-active,
.slide-leave-active {
  transition: all 0.2s ease;
}

.slide-enter-from,
.slide-leave-to {
  opacity: 0;
  max-height: 0;
}

.slide-enter-to,
.slide-leave-from {
  opacity: 1;
  max-height: 400px;
}

.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.line-clamp-4 {
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
