<template>
  <div class="min-h-screen bg-stone-950 text-stone-50">
    <div :class="settingsOpen ? 'pointer-events-none' : ''">
      <header class="sticky top-0 z-20 border-b border-stone-800/40 bg-stone-950/95 backdrop-blur">
        <div class="mx-auto flex h-14 w-full max-w-3xl items-center justify-between px-4">
          <!-- Left: menu + title -->
          <div class="flex min-w-0 flex-1 items-center gap-3">
            <button
              class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-stone-300 transition hover:bg-stone-800"
              @click="drawerOpen = true"
            >
              <Menu class="h-5 w-5" />
            </button>
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
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
              v-if="activeSessionId"
              class="flex h-10 w-10 items-center justify-center rounded-lg text-stone-400 transition hover:bg-stone-800 hover:text-stone-200 disabled:opacity-50"
              :disabled="syncing"
              @click="handleSync"
              title="Sync messages from CLI"
            >
              <RefreshCw class="h-5 w-5" :class="{ 'animate-spin': syncing }" />
            </button>

            <div class="relative" ref="menuRef">
              <button
                v-if="activeSessionId"
                class="flex h-10 w-10 items-center justify-center rounded-lg text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
                @click="toggleSessionMenu"
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
                    @click="openRename"
                  >
                    Rename
                  </button>
                  <button
                    class="w-full px-3 py-2 text-left text-sm text-stone-300 transition hover:bg-stone-800"
                    @click="openInfo"
                  >
                    Session info
                  </button>
                </div>
              </transition>
            </div>
          </div>
        </div>
      </header>

      <main class="mx-auto w-full max-w-3xl px-4 pb-32 pt-4">
        <RouterView />
      </main>

    <Sheet :open="drawerOpen" @update:open="drawerOpen = $event">
      <SheetContent side="left" class="flex w-full max-w-[280px] flex-col border-stone-800/50 bg-stone-900 p-0 text-stone-50 [&>button]:hidden">
        <!-- Header with new session and attach buttons -->
        <div class="flex items-center justify-between border-b border-stone-800/50 px-4 py-3">
          <span class="text-sm font-medium text-stone-200">Tether</span>
          <div class="flex items-center gap-1">
            <button
              class="flex h-8 w-8 items-center justify-center rounded-lg text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
              @click="openExternalBrowser"
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
            <div v-if="directoryGroups.length > 1" class="flex gap-1">
              <button
                class="flex h-9 w-9 items-center justify-center rounded-lg bg-stone-800/50 text-stone-400 transition hover:bg-stone-800 hover:text-stone-300"
                @click="expandAllDirectories"
                title="Expand all"
              >
                <ChevronsUpDown class="h-4 w-4" />
              </button>
              <button
                class="flex h-9 w-9 items-center justify-center rounded-lg bg-stone-800/50 text-stone-400 transition hover:bg-stone-800 hover:text-stone-300"
                @click="collapseAllDirectories"
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
              <div class="relative">
                <Input
                  v-model="directoryInput"
                  placeholder="/path/to/project"
                  class="h-9 border-stone-700 bg-stone-900/50 pr-8 text-sm"
                />
                <div class="absolute right-2 top-1/2 -translate-y-1/2">
                  <GitBranch
                    v-if="directoryProbe?.exists && directoryProbe?.is_git"
                    class="h-4 w-4 text-emerald-400"
                    title="Git repository"
                  />
                  <Folder
                    v-else-if="directoryProbe?.exists"
                    class="h-4 w-4 text-stone-400"
                    title="Directory (no git)"
                  />
                </div>
              </div>
              <button
                class="w-full rounded-lg bg-emerald-600 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-50"
                :disabled="checkingDirectory || !directoryInput.trim() || !directoryProbe?.exists"
                @click="createDirectorySession"
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
          <div v-for="group in filteredDirectoryGroups" :key="group.key" class="mb-2">
            <!-- Directory header (clickable to toggle) -->
            <button
              class="mb-0.5 flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left transition hover:bg-stone-800/50"
              @click="toggleDirectory(group.key)"
            >
              <div class="flex min-w-0 flex-1 items-center gap-2">
                <ChevronRight
                  class="h-3.5 w-3.5 shrink-0 text-stone-500 transition-transform duration-200"
                  :class="{ 'rotate-90': isDirectoryExpanded(group.key) }"
                />
                <Folder class="h-3.5 w-3.5 shrink-0 text-stone-500" />
                <span class="truncate text-xs font-medium text-stone-400">{{ group.label }}</span>
                <GitBranch v-if="group.hasGit" class="h-3 w-3 shrink-0 text-emerald-500" />
                <span class="shrink-0 text-[10px] text-stone-600">{{ group.sessions.length }}</span>
              </div>
              <button
                v-if="group.path"
                class="flex h-6 w-6 shrink-0 items-center justify-center rounded text-stone-500 transition hover:bg-stone-700 hover:text-stone-300"
                @click.stop="addSessionToDirectory(group.path)"
                :disabled="creating"
                title="Add session"
              >
                <Plus class="h-3.5 w-3.5" />
              </button>
            </button>

            <!-- Sessions in this directory (collapsible) -->
            <div v-if="isDirectoryExpanded(group.key)" class="space-y-0.5 pl-5">
              <button
                v-for="session in group.sessions"
                :key="session.id"
                class="group flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition"
                :class="session.id === activeSessionId
                  ? 'bg-stone-800 text-stone-100'
                  : 'text-stone-300 hover:bg-stone-800/50'"
                @click="selectSession(session.id)"
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
                      :class="{
                        'text-emerald-400': session.state === 'RUNNING',
                        'text-amber-400': session.state === 'AWAITING_INPUT',
                        'text-orange-400': session.state === 'INTERRUPTING',
                        'text-rose-400': session.state === 'ERROR'
                      }"
                    >
                      <span
                        class="h-1.5 w-1.5 rounded-full"
                        :class="{
                          'bg-emerald-400': session.state === 'RUNNING',
                          'bg-amber-400': session.state === 'AWAITING_INPUT',
                          'bg-orange-400': session.state === 'INTERRUPTING',
                          'bg-rose-500': session.state === 'ERROR',
                          'animate-pulse': session.state === 'RUNNING' || session.state === 'AWAITING_INPUT'
                        }"
                      ></span>
                      {{ formatState(session.state) }}
                    </span>
                  </div>
                </div>
                <button
                  class="flex h-6 w-6 shrink-0 items-center justify-center rounded text-stone-500 opacity-0 transition hover:bg-stone-700 hover:text-rose-400 group-hover:opacity-100"
                  @click.stop="removeSession(session.id)"
                  :disabled="deleting"
                  title="Delete"
                >
                  <X class="h-3.5 w-3.5" />
                </button>
              </button>
            </div>
          </div>

          <p v-if="!filteredDirectoryGroups.length && searchQuery.trim()" class="px-2 text-center text-xs text-stone-500">
            No matches
          </p>
          <p v-else-if="!directoryGroups.length" class="px-2 text-center text-xs text-stone-500">
            No sessions yet
          </p>
        </div>

        <!-- Footer -->
        <div class="border-t border-stone-800/50 px-3 py-3">
          <button
            class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-sm text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
            @click="openSettings"
          >
            <SettingsIcon class="h-4 w-4" />
            Settings
          </button>
        </div>
      </SheetContent>
    </Sheet>

    <!-- External session browser -->
    <ExternalSessionBrowser
      :open="externalBrowserOpen"
      @update:open="externalBrowserOpen = $event"
      @attached="handleSessionAttached"
    />
    </div>

    <!-- Settings dialog -->
    <Dialog :open="settingsOpen" @update:open="settingsOpen = $event">
      <DialogContent class="max-w-md border-stone-800 bg-stone-900">
        <DialogHeader>
          <DialogTitle class="text-stone-100">Settings</DialogTitle>
        </DialogHeader>
        <Settings />
      </DialogContent>
    </Dialog>

    <!-- Auth dialog -->
    <Dialog :open="authModalOpen" @update:open="authModalOpen = $event">
      <DialogContent class="max-w-sm border-stone-800 bg-stone-900">
        <DialogHeader>
          <DialogTitle class="text-stone-100">Connect</DialogTitle>
        </DialogHeader>
        <div class="space-y-4">
          <p class="text-sm text-stone-400">
            Enter your token to connect.
          </p>
          <Input
            v-model="tokenInput"
            type="password"
            placeholder="AGENT_TOKEN"
            class="border-stone-700 bg-stone-800"
          />
          <button
            class="w-full rounded-lg bg-emerald-600 py-2.5 text-sm font-medium text-white transition hover:bg-emerald-500"
            @click="saveToken"
          >
            {{ tokenSaved ? 'Saved!' : 'Save token' }}
          </button>
        </div>
      </DialogContent>
    </Dialog>

    <!-- Onboarding -->
    <transition name="fade">
      <div
        v-if="showOnboarding"
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
              @click="openExternalBrowser"
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
            <div class="relative">
              <Input
                v-model="directoryInput"
                placeholder="/path/to/project"
                class="border-stone-700 bg-stone-800 pr-8"
              />
              <div class="absolute right-3 top-1/2 -translate-y-1/2">
                <GitBranch
                  v-if="directoryProbe?.exists && directoryProbe?.is_git"
                  class="h-4 w-4 text-emerald-400"
                />
                <Folder
                  v-else-if="directoryProbe?.exists"
                  class="h-4 w-4 text-stone-400"
                />
              </div>
            </div>
            <button
              class="w-full rounded-lg border border-stone-700 bg-stone-800/50 py-2.5 text-sm font-medium text-stone-300 transition hover:bg-stone-800 disabled:opacity-50"
              @click="createDirectorySession"
              :disabled="!directoryProbe?.exists || checkingDirectory || creating"
            >
              {{ checkingDirectory ? 'Checking...' : 'Start session' }}
            </button>
            <p v-if="directoryProbe && !directoryProbe.exists" class="text-center text-xs text-rose-400">
              Directory not found
            </p>
            <p v-else-if="directoryError" class="text-center text-xs text-rose-400">{{ directoryError }}</p>
          </div>
        </div>
      </div>
    </transition>

    <!-- Connection error (full screen) -->
    <transition name="fade">
      <div
        v-if="isConnectionError && !sessions.length"
        class="fixed inset-0 z-50 flex items-center justify-center bg-stone-950/95 px-4"
      >
        <div class="w-full max-w-sm text-center">
          <div class="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-rose-950/50">
            <svg class="h-8 w-8 text-rose-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
          </div>
          <h2 class="text-xl font-semibold text-stone-100">Unable to connect</h2>
          <p class="mt-2 text-sm text-stone-400">
            The agent server is not responding. Make sure it's running and try again.
          </p>
          <button
            class="mt-6 w-full rounded-lg bg-stone-800 py-2.5 text-sm font-medium text-stone-200 transition hover:bg-stone-700"
            @click="refreshSessions"
          >
            Retry connection
          </button>
        </div>
      </div>
    </transition>

    <!-- Error toast (for non-connection errors) -->
    <transition name="slide-up">
      <div
        v-if="error && !isConnectionError"
        class="fixed bottom-20 left-4 right-4 z-50 mx-auto max-w-md rounded-xl border border-rose-500/30 bg-rose-950/90 px-4 py-3 text-sm text-rose-200 shadow-lg backdrop-blur"
      >
        {{ error }}
      </div>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterView, useRoute, useRouter } from "vue-router";
import { Folder, Menu, GitBranch, MoreVertical, Plus, Settings as SettingsIcon, X, Link, RefreshCw, ChevronRight, Clock, MessageSquare, ChevronsDownUp, ChevronsUpDown } from "lucide-vue-next";
import {
  createSession,
  deleteSession,
  interruptSession,
  listSessions,
  checkDirectory,
  syncSession,
  AUTH_REQUIRED_EVENT,
  getToken,
  setToken,
  type DirectoryCheck,
  type Session
} from "./api";
import { activeSessionId, requestInfo, requestRename } from "./state";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle
} from "@/components/ui/dialog";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { Input } from "@/components/ui/input";
import Settings from "./views/Settings.vue";
import ExternalSessionBrowser from "@/components/external/ExternalSessionBrowser.vue";

const router = useRouter();
const route = useRoute();

const drawerOpen = ref(false);
const sessions = ref<Session[]>([]);
const searchQuery = ref("");
const creating = ref(false);
const deleting = ref(false);
const error = ref("");
const loaded = ref(false);
const authRequired = ref(false);
const authModalOpen = ref(false);
const directoryInput = ref("");
const checkingDirectory = ref(false);
const directoryProbe = ref<DirectoryCheck | null>(null);
const directoryError = ref("");
const createPanelOpen = ref(false);
let directoryTimer: number | null = null;
const settingsOpen = ref(false);
const externalBrowserOpen = ref(false);
const menuOpen = ref(false);
const expandedDirectories = ref(new Set<string>());
const menuRef = ref<HTMLElement | null>(null);
const syncing = ref(false);
const menuHandler = (event: MouseEvent | TouchEvent) => {
  if (!menuOpen.value) {
    return;
  }
  if (menuRef.value && menuRef.value.contains(event.target as Node)) {
    return;
  }
  menuOpen.value = false;
};
const handleAuthRequired = () => {
  error.value = "Authentication required. Enter your token to connect.";
  authRequired.value = true;
  tokenInput.value = getToken();
  authModalOpen.value = true;
};

const tokenInput = ref(getToken());
const tokenSaved = ref(false);
const saveToken = () => {
  setToken(tokenInput.value.trim());
  tokenSaved.value = true;
  setTimeout(() => {
    tokenSaved.value = false;
  }, 1200);
  authModalOpen.value = false;
  refreshSessions().catch(() => undefined);
};

const activeSession = computed(() =>
  sessions.value.find((session) => session.id === activeSessionId.value)
);

const formatState = (state: string | undefined): string => {
  if (!state) return "";
  const labels: Record<string, string> = {
    CREATED: "Ready",
    RUNNING: "Running",
    AWAITING_INPUT: "Awaiting input",
    INTERRUPTING: "Interrupting",
    ERROR: "Error"
  };
  return labels[state] || state.toLowerCase().replace(/_/g, " ");
};

const formatTime = (timestamp: string): string => {
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
};

const formatSessionId = (id: string): string => {
  return id.slice(0, 8);
};

const statusDot = computed(() => {
  switch (activeSession.value?.state) {
    case "RUNNING":
      return "bg-emerald-500";
    case "AWAITING_INPUT":
      return "bg-amber-400 animate-pulse";
    case "INTERRUPTING":
      return "bg-amber-500";
    case "ERROR":
      return "bg-rose-500";
    case "CREATED":
      return "bg-blue-400";
    default:
      return "";
  }
});

const isConnectionError = computed(() => {
  const msg = error.value.toLowerCase();
  return msg.includes("failed to fetch") ||
    msg.includes("networkerror") ||
    msg.includes("500") ||
    msg.includes("502") ||
    msg.includes("503") ||
    msg.includes("connection");
});

const showOnboarding = computed(
  () => loaded.value && !sessions.value.length && !creating.value && !authRequired.value && !error.value
);

const openSettings = () => {
  settingsOpen.value = true;
};

const openExternalBrowser = () => {
  externalBrowserOpen.value = true;
};

const handleSessionAttached = async (sessionId: string) => {
  // Refresh sessions list and select the new session
  await refreshSessions();
  activeSessionId.value = sessionId;
  drawerOpen.value = false;
};

const closeSettings = () => {
  settingsOpen.value = false;
};

const maybeSelectDefaultSession = (list: Session[]) => {
  // If there's a session ID in the URL, use that
  const routeId = route.params.id as string | undefined;
  if (routeId) {
    // Only set if the session exists in the list
    const exists = list.some((s) => s.id === routeId);
    if (exists) {
      activeSessionId.value = routeId;
      return;
    }
    // Session doesn't exist, clear URL and fall through to default selection
    router.replace({ path: "/" });
  }
  // Otherwise select first session if none selected
  if (!activeSessionId.value && list.length) {
    activeSessionId.value = list[0].id;
  }
};

const formatDirectoryLabel = (dir: string | null) => {
  if (!dir) {
    return "Temporary workspace";
  }
  const trimmed = dir.replace(/[\\/]+$/, "");
  const segments = trimmed.split(/[\\/]/).filter(Boolean);
  return segments.at(-1) || trimmed;
};

const directoryGroups = computed(() => {
  const map = new Map<string, { key: string; label: string; path: string | null; sessions: Session[]; hasGit: boolean }>();
  sessions.value.forEach((session) => {
    const key = session.directory ?? session.id;
    if (!map.has(key)) {
      map.set(key, {
        key,
        label: formatDirectoryLabel(session.directory),
        path: session.directory,
        sessions: [],
        hasGit: Boolean(session.directory_has_git)
      });
    }
    const group = map.get(key)!;
    group.sessions.push(session);
    if (session.directory_has_git) {
      group.hasGit = true;
    }
  });
  return Array.from(map.values());
});

const filteredDirectoryGroups = computed(() => {
  const query = searchQuery.value.trim().toLowerCase();
  if (!query) {
    return directoryGroups.value;
  }
  return directoryGroups.value
    .map((group) => {
      const labelMatch = group.label.toLowerCase().includes(query);
      const pathMatch = group.path?.toLowerCase().includes(query);
      // Also filter sessions within the group by session ID
      const matchingSessions = group.sessions.filter((session) => {
        const sessionIdMatch = (session.runner_session_id || session.id).toLowerCase().includes(query);
        const nameMatch = session.name?.toLowerCase().includes(query);
        return sessionIdMatch || nameMatch;
      });
      // Include group if label/path matches (show all sessions) or if any session matches
      if (labelMatch || pathMatch) {
        return group;
      }
      if (matchingSessions.length > 0) {
        return { ...group, sessions: matchingSessions };
      }
      return null;
    })
    .filter((group): group is NonNullable<typeof group> => group !== null);
});

// Get the directory key for the active session
const activeSessionDirectoryKey = computed(() => {
  const session = activeSession.value;
  if (!session) return null;
  return session.directory ?? session.id;
});

// Check if a directory is expanded
const isDirectoryExpanded = (key: string) => {
  return expandedDirectories.value.has(key);
};

// Toggle directory expansion
const toggleDirectory = (key: string) => {
  if (expandedDirectories.value.has(key)) {
    expandedDirectories.value.delete(key);
  } else {
    expandedDirectories.value.add(key);
  }
};

// Expand all directories
const expandAllDirectories = () => {
  directoryGroups.value.forEach((group) => {
    expandedDirectories.value.add(group.key);
  });
};

// Collapse all directories
const collapseAllDirectories = () => {
  expandedDirectories.value.clear();
};

const refreshSessions = async () => {
  error.value = "";
  try {
    const fetched = await listSessions();
    sessions.value = fetched;
    maybeSelectDefaultSession(fetched);
    authRequired.value = false;
    // Auto-expand the active session's directory
    const activeId = activeSessionId.value;
    if (activeId) {
      const session = fetched.find((s) => s.id === activeId);
      if (session) {
        const dirKey = session.directory ?? session.id;
        expandedDirectories.value.add(dirKey);
      }
    }
  } catch (err) {
    error.value = String(err);
  } finally {
    loaded.value = true;
  }
};

const createDirectorySession = async () => {
  const path = directoryInput.value.trim();
  if (!path) {
    directoryError.value = "Provide a directory";
    return;
  }
  let status = directoryProbe.value;
  if (!status || status.path !== path) {
    checkingDirectory.value = true;
    try {
      status = await checkDirectory(path);
      directoryProbe.value = status;
    } catch (err) {
      directoryError.value = String(err);
      checkingDirectory.value = false;
      return;
    } finally {
      checkingDirectory.value = false;
    }
  }
  if (!status.exists) {
    directoryError.value = "Directory not found";
    return;
  }
  await createSessionForPath(path, { closeDrawer: true });
};

const selectSession = (id: string) => {
  activeSessionId.value = id;
  drawerOpen.value = false;
};

const createSessionForPath = async (
  path: string,
  options: { closeDrawer?: boolean } = {}
) => {
  creating.value = true;
  error.value = "";
  try {
    const created = await createSession({ directory: path });
    activeSessionId.value = created.id;
    await refreshSessions();
    directoryError.value = "";
    if (options.closeDrawer) {
      drawerOpen.value = false;
      createPanelOpen.value = false;
    }
  } catch (err) {
    error.value = String(err);
  } finally {
    creating.value = false;
  }
};

const addSessionToDirectory = async (path: string | null) => {
  if (!path) {
    return;
  }
  await createSessionForPath(path, { closeDrawer: false });
};

const toggleSessionMenu = () => {
  menuOpen.value = !menuOpen.value;
};

const openRename = () => {
  if (!activeSessionId.value) {
    return;
  }
  requestRename.value += 1;
  menuOpen.value = false;
};

const openInfo = () => {
  if (!activeSessionId.value) {
    return;
  }
  requestInfo.value += 1;
  menuOpen.value = false;
};

const handleSync = async () => {
  if (!activeSessionId.value || syncing.value) return;
  syncing.value = true;
  error.value = "";
  try {
    const result = await syncSession(activeSessionId.value);
    if (result.synced > 0) {
      console.log(`Synced ${result.synced} new messages`);
    }
  } catch (err) {
    // 400 means not an attached session, 404 means external session not found
    const msg = String(err);
    if (!msg.includes("400") && !msg.includes("404")) {
      error.value = msg;
    }
  } finally {
    syncing.value = false;
  }
};

const removeSession = async (id: string) => {
  if (deleting.value) {
    return;
  }
  deleting.value = true;
  error.value = "";
  try {
    const target = sessions.value.find((session) => session.id === id);
    if (target?.state === "RUNNING") {
      await interruptSession(id);
    }
    await deleteSession(id);
    if (activeSessionId.value === id) {
      activeSessionId.value = null;
    }
    await refreshSessions();
  } catch (err) {
    error.value = String(err);
  } finally {
    deleting.value = false;
  }
};

const scheduleDirectoryCheck = (value: string) => {
  if (directoryTimer) {
    window.clearTimeout(directoryTimer);
  }
  const trimmed = value.trim();
  if (!trimmed) {
    directoryProbe.value = null;
    directoryError.value = "";
    checkingDirectory.value = false;
    return;
  }
  checkingDirectory.value = true;
  directoryTimer = window.setTimeout(async () => {
    try {
      const status = await checkDirectory(trimmed);
      directoryProbe.value = status;
      if (!status.exists) {
        directoryError.value = "Directory not found";
      } else {
        directoryError.value = "";
      }
    } catch (err) {
      directoryProbe.value = null;
      directoryError.value = String(err);
    } finally {
      checkingDirectory.value = false;
      directoryTimer = null;
    }
  }, 400);
};

watch(directoryInput, (value) => {
  scheduleDirectoryCheck(value);
});

watch(drawerOpen, (open) => {
  if (open) {
    refreshSessions().catch(() => undefined);
  }
});

watch(activeSessionId, (newId, oldId) => {
  if (newId === oldId) {
    return;
  }
  refreshSessions().catch(() => undefined);
  // Auto-sync messages from CLI when opening a session
  if (newId) {
    handleSync();
  }
  // Auto-expand the directory containing the active session
  const session = sessions.value.find((s) => s.id === newId);
  if (session) {
    const dirKey = session.directory ?? session.id;
    expandedDirectories.value.add(dirKey);
  }
  // Sync URL when session changes
  const routeId = route.params.id as string | undefined;
  if (newId && newId !== routeId) {
    router.replace({ name: "session", params: { id: newId } });
  } else if (!newId && routeId) {
    router.replace({ path: "/" });
  }
});

// Sync activeSessionId from URL when route changes (e.g., browser back/forward)
watch(
  () => route.params.id,
  (newRouteId) => {
    const id = newRouteId as string | undefined;
    if (id && id !== activeSessionId.value) {
      activeSessionId.value = id;
    }
  }
);

onMounted(refreshSessions);
onMounted(() => {
  window.addEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
  window.addEventListener("click", menuHandler);
  window.addEventListener("touchstart", menuHandler);
});

onUnmounted(() => {
  if (directoryTimer) {
    window.clearTimeout(directoryTimer);
  }
  window.removeEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
  window.removeEventListener("click", menuHandler);
  window.removeEventListener("touchstart", menuHandler);
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

.slide-up-enter-active,
.slide-up-leave-active {
  transition: all 0.3s ease;
}
.slide-up-enter-from,
.slide-up-leave-to {
  opacity: 0;
  transform: translateY(16px);
}
</style>
