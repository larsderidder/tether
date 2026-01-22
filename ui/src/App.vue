<template>
  <div class="min-h-screen bg-stone-950 text-stone-50">
    <div :class="settingsOpen ? 'pointer-events-none' : ''">
      <header class="sticky top-0 z-10 border-b border-stone-800/60 bg-stone-950/90 backdrop-blur">
      <div class="mx-auto flex w-full max-w-5xl items-center justify-between gap-4 px-4 py-3">
        <div class="flex items-center gap-3">
          <Button variant="ghost" size="icon" class="h-9 w-9 text-stone-50" @click="drawerOpen = true">
            <Menu class="h-5 w-5" />
          </Button>
          <div class="flex items-center gap-3">
            <p class="text-base font-semibold tracking-wide">Tether</p>
            <div v-if="sessionTitle" class="flex items-center gap-2">
              <span v-if="statusDot" class="h-2 w-2 rounded-full" :class="statusDot"></span>
              <p class="text-base font-semibold text-stone-100">
                {{ sessionTitle }}
              </p>
              <p v-if="sessionDirectory" class="text-sm text-stone-400" :title="fullSessionDirectory">
                {{ sessionDirectory }}
              </p>
            </div>
          </div>
        </div>
        <div class="relative" ref="menuRef">
          <Button
            variant="ghost"
            size="icon"
            class="h-9 w-9 text-stone-50"
            @click="toggleSessionMenu"
            title="Session actions"
          >
            <MoreVertical class="h-5 w-5" />
          </Button>
          <div
            v-if="menuOpen"
            class="absolute right-0 mt-2 w-44 rounded-2xl border border-stone-800 bg-stone-950 p-1 shadow-2xl"
          >
            <Button
              variant="ghost"
              size="sm"
              class="w-full justify-start text-sm"
              :disabled="!activeSessionId"
              @click="openRename"
            >
              Rename session
            </Button>
            <Button
              variant="ghost"
              size="sm"
              class="w-full justify-start text-sm"
              :disabled="!activeSessionId"
              @click="openInfo"
            >
              Session info
            </Button>
          </div>
        </div>
      </div>
    </header>

    <main class="mx-auto w-full max-w-5xl px-4 pb-24 pt-4">
      <RouterView />
    </main>

    <Sheet :open="drawerOpen" @update:open="drawerOpen = $event">
      <SheetContent side="left" class="w-full max-w-xs border-stone-800 bg-stone-950 text-stone-50">
        <SheetHeader>
          <SheetTitle class="text-stone-50">Sessions</SheetTitle>
          <SheetDescription class="text-sm text-stone-400">Grouped by directory.</SheetDescription>
        </SheetHeader>
        <div class="px-3">
          <Button
            size="sm"
            variant="outline"
            class="w-full"
            @click="createPanelOpen = !createPanelOpen"
          >
            {{ createPanelOpen ? "Hide start options" : "Start a new session" }}
          </Button>
          <transition name="fade">
            <div
              v-if="createPanelOpen"
              class="mt-3 space-y-3 rounded-2xl border border-stone-800/60 bg-stone-900/60 p-3"
            >
              <div class="flex items-center gap-2 text-sm text-stone-300">
                <Folder class="h-4 w-4" />
                <p class="text-xs uppercase tracking-[0.2em]">Start from an existing directory</p>
              </div>
              <Input v-model="directoryInput" size="sm" placeholder="/path/to/project" />
              <div class="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  :disabled="checkingDirectory || !directoryInput.trim()"
                  @click="createDirectorySession"
                >
                  Use directory
                </Button>
              </div>
              <p class="text-[11px] text-stone-400">
                <span v-if="checkingDirectory">Checking directory…</span>
                <span v-else-if="directoryProbe">
                  <span v-if="directoryProbe.exists">
                    <span v-if="directoryProbe.is_git">Git repo detected.</span>
                    <span v-else>No git repository detected.</span>
                  </span>
                  <span v-else>Directory unavailable.</span>
                </span>
                <span v-else>Provide a path to see status.</span>
              </p>
              <p v-if="directoryError" class="text-[11px] text-rose-400">{{ directoryError }}</p>
            </div>
          </transition>
        </div>
        <div class="space-y-3 px-3 pb-4">
          <div
            v-for="group in directoryGroups"
            :key="group.key"
            class="rounded-2xl border border-stone-800/60 bg-stone-900/70 p-3"
            :title="group.path || 'Temporary workspace'"
          >
              <div class="flex items-start justify-between gap-3">
                <div>
                  <p class="text-sm font-semibold text-stone-50">{{ group.label }}</p>
                  <p class="text-xs text-stone-400">{{ group.path || "Temporary workspace" }}</p>
                </div>
                <div class="flex items-center gap-2">
                  <Button
                    size="icon"
                    variant="ghost"
                    class="h-7 w-7 text-stone-400 hover:text-stone-100"
                    :disabled="creating || !group.path"
                    @click="addSessionToDirectory(group.path)"
                    title="Add session"
                  >
                    <Plus class="h-4 w-4" />
                  </Button>
                  <Badge
                    variant="outline"
                    class="text-[10px] uppercase tracking-[0.2em]"
                    v-if="group.hasGit"
                    title="Git repository"
                  >
                    <GitBranch class="h-3 w-3 text-emerald-300" />
                  </Badge>
                  <Badge v-else variant="outline" class="text-[10px] uppercase tracking-[0.2em] text-stone-400">
                    No git
                  </Badge>
                </div>
              </div>
            <ul class="mt-3 space-y-2">
              <li v-for="session in group.sessions" :key="session.id">
                <div
                  class="flex items-center justify-between gap-3 rounded-xl px-3 py-2 text-left text-sm transition hover:bg-stone-800/80"
                  :class="{
                    'border border-emerald-400/60 bg-emerald-500/10 text-emerald-200': session.id === activeSessionId
                  }"
                >
                  <button class="flex flex-1 items-center justify-between gap-3" @click="selectSession(session.id)">
                    <div class="space-y-0.5">
                      <p class="font-semibold">{{ session.name || session.directory || "Session" }}</p>
                      <div class="flex items-center gap-2">
                        <p class="text-[10px] uppercase tracking-[0.3em] text-stone-400">{{ session.state }}</p>
                        <span
                          v-if="session.runner_type"
                          class="rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase"
                          :class="session.runner_type === 'claude'
                            ? 'bg-amber-900/40 text-amber-300'
                            : 'bg-emerald-900/40 text-emerald-300'"
                        >
                          {{ session.runner_type }}
                        </span>
                      </div>
                    </div>
                    <span class="text-[10px] text-stone-400">{{ session.id.slice(-6) }}</span>
                  </button>
                  <Button
                    size="icon"
                    variant="ghost"
                    class="h-7 w-7 text-stone-400 hover:text-rose-300"
                    @click="removeSession(session.id)"
                    :disabled="deleting"
                    title="Delete session"
                  >
                    <X class="h-4 w-4" />
                  </Button>
                </div>
              </li>
            </ul>
          </div>
          <p v-if="!directoryGroups.length" class="text-xs text-stone-500">No sessions yet.</p>
        </div>
        <div class="space-y-2 border-t border-stone-800/60 px-3 py-3">
          <Button size="sm" variant="outline" class="w-full" @click="openSettings">
            Settings
          </Button>
        </div>
      </SheetContent>
    </Sheet>
    </div>

    <transition name="fade">
      <div
        v-if="settingsOpen"
        class="fixed inset-0 z-[60] flex items-center justify-center bg-stone-950/90 px-4"
      >
        <Card class="w-full max-w-lg space-y-5 rounded-3xl border border-stone-800/80 bg-stone-900/90 p-5 shadow-2xl">
          <div class="flex items-start justify-between gap-4">
            <div class="space-y-1">
              <p class="text-xs uppercase tracking-[0.4em] text-stone-500">Settings</p>
              <p class="text-lg font-semibold text-stone-100">Tether preferences</p>
            </div>
            <Button variant="ghost" size="icon" @click="closeSettings">
              <X class="h-4 w-4" />
            </Button>
          </div>
          <Settings />
        </Card>
      </div>
    </transition>

    <transition name="fade">
      <div
        v-if="authModalOpen"
        class="fixed inset-0 z-[70] flex items-center justify-center bg-stone-950/90 px-4"
      >
        <Card class="w-full max-w-md space-y-4 border border-stone-800/80 bg-stone-900/90 p-4">
          <div class="flex items-center justify-between gap-4">
            <p class="text-sm font-semibold uppercase tracking-[0.3em] text-stone-400">Welcome</p>
            <Button variant="ghost" size="icon" @click="authModalOpen = false">
              <X class="h-4 w-4" />
            </Button>
          </div>
          <div class="space-y-2">
            <p class="text-sm text-stone-300">
              Enter your AGENT_TOKEN to connect to this agent.
            </p>
            <Input v-model="tokenInput" placeholder="AGENT_TOKEN" />
          </div>
          <div class="flex items-center gap-3">
            <Button @click="saveToken">Save token</Button>
            <span v-if="tokenSaved" class="text-sm text-emerald-400">Saved.</span>
          </div>
        </Card>
      </div>
    </transition>

    <transition name="fade">
      <div
        v-if="showOnboarding"
        class="fixed inset-0 z-40 flex items-center justify-center bg-stone-950/80 px-4"
      >
        <Card class="w-full max-w-md space-y-4 border border-stone-800/80 bg-stone-900/80">
          <CardContent class="space-y-4">
            <div class="space-y-2">
              <p class="text-lg font-semibold text-stone-50">Start your first agent</p>
              <p class="text-sm text-stone-400">
                Pick a directory to work in.
              </p>
            </div>
          <div class="space-y-2">
            <div class="flex flex-col gap-2">
              <Input v-model="directoryInput" size="sm" placeholder="/path/to/project" />
              <Button
                class="w-full"
                @click="createDirectorySession"
                :disabled="!directoryProbe?.exists || checkingDirectory || creating"
              >
                Use directory
              </Button>
            </div>
              <p v-if="directoryError" class="text-[11px] text-rose-400">{{ directoryError }}</p>
              <p v-else class="text-[11px] text-stone-400">
                Git status:
                <span v-if="directoryProbe">
                  {{ directoryProbe.is_git ? "Git repo" : "No git repo" }}
                </span>
                <span v-else>Awaiting input</span>.
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    </transition>

    <p
      v-if="error"
      class="fixed bottom-4 left-4 right-4 rounded-2xl border border-rose-500/70 bg-rose-500/10 px-4 py-2 text-sm text-rose-200"
    >
      {{ error }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterView } from "vue-router";
import { Folder, Menu, GitBranch, MoreVertical, Plus, X } from "lucide-vue-next";
import {
  createSession,
  deleteSession,
  stopSession,
  listSessions,
  checkDirectory,
  AUTH_REQUIRED_EVENT,
  getToken,
  setToken,
  type DirectoryCheck,
  type Session
} from "./api";
import { activeSessionId, requestInfo, requestRename } from "./state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle
} from "@/components/ui/sheet";
import { Input } from "@/components/ui/input";
import Settings from "./views/Settings.vue";

const drawerOpen = ref(false);
const sessions = ref<Session[]>([]);
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
const menuOpen = ref(false);
const menuRef = ref<HTMLElement | null>(null);
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

const sessionTitle = computed(
  () => activeSession.value?.name || activeSession.value?.directory || ""
);

const sessionDirectory = computed(() => {
  const raw = activeSession.value?.directory || "";
  if (!raw) {
    return "";
  }
  const trimmed = raw.replace(/[\\/]+$/, "");
  const segments = trimmed.split(/[\\/]/).filter(Boolean);
  return segments.at(-1) || trimmed;
});

const fullSessionDirectory = computed(
  () => activeSession.value?.directory || ""
);

const statusDot = computed(() => {
  switch (activeSession.value?.state) {
    case "RUNNING":
      return "bg-emerald-500";
    case "AWAITING_INPUT":
      return "bg-amber-400 animate-pulse";
    case "STOPPING":
      return "bg-amber-500";
    case "ERROR":
      return "bg-rose-500";
    case "STOPPED":
      return "bg-stone-400";
    case "CREATED":
      return "bg-blue-400";
    default:
      return "";
  }
});

const showOnboarding = computed(
  () => loaded.value && !sessions.value.length && !creating.value && !authRequired.value
);

const openSettings = () => {
  settingsOpen.value = true;
};

const closeSettings = () => {
  settingsOpen.value = false;
};

const maybeSelectDefaultSession = (list: Session[]) => {
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

const refreshSessions = async () => {
  error.value = "";
  try {
    const fetched = await listSessions();
    sessions.value = fetched;
    maybeSelectDefaultSession(fetched);
    authRequired.value = false;
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

const removeSession = async (id: string) => {
  if (deleting.value) {
    return;
  }
  deleting.value = true;
  error.value = "";
  try {
    const target = sessions.value.find((session) => session.id === id);
    if (target?.state === "RUNNING") {
      await stopSession(id);
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
});

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
