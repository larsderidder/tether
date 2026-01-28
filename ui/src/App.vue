<template>
  <div class="app-shell">
    <div :class="['app-content', settingsOpen ? 'pointer-events-none' : '']">
      <AppHeader
        :active-session="activeSession"
        :has-active-session="!!activeSessionId"
        :syncing="syncing"
        :status-dot="statusDot"
        @open-drawer="drawerOpen = true"
        @sync="handleSync"
        @rename="openRename"
        @info="openInfo"
      />

      <main class="app-main" ref="scrollContainer">
        <div class="mx-auto w-full max-w-3xl px-4 pt-4">
          <RouterView />
        </div>
      </main>

      <!-- InputBar teleports here, outside the scroll container -->
      <div id="input-bar-slot"></div>

      <SessionDrawer
        v-model:open="drawerOpen"
        v-model:directory-input="directoryInput"
        :sessions="sessions"
        :active-session-id="activeSessionId"
        :creating="creating"
        :deleting="deleting"
        :checking-directory="checkingDirectory"
        :directory-probe="directoryProbe"
        :directory-error="directoryError"
        @select="handleSessionSelect"
        @delete="removeSession"
        @create="createDirectorySession"
        @attach="openExternalBrowser"
        @add-to-directory="addSessionToDirectory"
        @settings="openSettings"
      />

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
          <DialogTitle class="text-stone-100">Welcome to Tether</DialogTitle>
        </DialogHeader>
        <div class="space-y-4">
          <p class="text-sm text-stone-400">
            Enter the token from your agent server to get started.
          </p>
          <Input
            v-model="tokenInput"
            type="password"
            placeholder="Token"
            class="border-stone-700 bg-stone-800"
          />
          <button
            class="w-full rounded-lg bg-emerald-600 py-2.5 text-sm font-medium text-white transition hover:bg-emerald-500"
            @click="saveToken"
          >
            {{ tokenSaved ? 'Connected!' : 'Connect' }}
          </button>
        </div>
      </DialogContent>
    </Dialog>

    <!-- Onboarding -->
    <OnboardingOverlay
      :visible="showOnboarding"
      :directory-input="directoryInput"
      :checking="checkingDirectory"
      :probe="directoryProbe"
      :error="directoryError"
      :creating="creating"
      @update:directory-input="directoryInput = $event"
      @attach="openExternalBrowser"
      @create="createDirectorySession"
    />

    <!-- Connection error -->
    <ConnectionErrorOverlay
      :visible="isConnectionError && !sessions.length"
      @retry="refreshSessions"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { disableBodyScroll, enableBodyScroll } from "body-scroll-lock";
import { RouterView } from "vue-router";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import Settings from "./views/Settings.vue";
import ExternalSessionBrowser from "@/components/external/ExternalSessionBrowser.vue";
import {
  AppHeader,
  SessionDrawer,
  OnboardingOverlay,
  ConnectionErrorOverlay
} from "@/components/layout";
import { requestInfo, requestRename } from "./state";
import {
  useSessions,
  useDirectoryCheck,
  useAuth,
  getStatusDotClass
} from "@/composables";

// Core session state
const {
  sessions,
  activeSessionId,
  activeSession,
  loaded,
  error,
  creating,
  deleting,
  syncing,
  isConnectionError,
  refresh: refreshSessions,
  create: createSession,
  remove: removeSession,
  select: selectSession,
  sync: handleSync
} = useSessions();

// Directory input validation
const {
  input: directoryInput,
  checking: checkingDirectory,
  probe: directoryProbe,
  error: directoryError
} = useDirectoryCheck();

// Auth modal
const {
  authRequired,
  modalOpen: authModalOpen,
  tokenInput,
  tokenSaved,
  saveToken: handleSaveToken
} = useAuth();

// Local UI state
const drawerOpen = ref(false);
const settingsOpen = ref(false);
const externalBrowserOpen = ref(false);
const scrollContainer = ref<HTMLElement | null>(null);

// Computed
const statusDot = computed(() => getStatusDotClass(activeSession.value?.state));

const showOnboarding = computed(
  () => loaded.value && !sessions.value.length && !creating.value && !authRequired.value && !error.value
);

// Actions
const openSettings = () => { settingsOpen.value = true; };
const openExternalBrowser = () => { externalBrowserOpen.value = true; };

const openRename = () => {
  if (!activeSessionId.value) return;
  requestRename.value += 1;
};

const openInfo = () => {
  if (!activeSessionId.value) return;
  requestInfo.value += 1;
};

const saveToken = () => {
  handleSaveToken();
};

const handleSessionSelect = (id: string) => {
  selectSession(id);
  drawerOpen.value = false;
};

const handleSessionAttached = async (sessionId: string) => {
  await refreshSessions();
  selectSession(sessionId);
  drawerOpen.value = false;
};

const createDirectorySession = async () => {
  const path = directoryInput.value.trim();
  if (!path || !directoryProbe.value?.exists) return;
  const created = await createSession(path);
  if (created) {
    drawerOpen.value = false;
  }
};

const addSessionToDirectory = async (path: string) => {
  await createSession(path);
};

// Watchers
watch(drawerOpen, (open) => {
  if (open) refreshSessions();
});

watch(activeSessionId, (newId, oldId) => {
  if (newId === oldId) return;
  refreshSessions();
  if (newId) handleSync();
});

// Lifecycle
onMounted(() => {
  refreshSessions();
  // Lock body scroll, allow only scrollContainer to scroll
  if (scrollContainer.value) {
    disableBodyScroll(scrollContainer.value, {
      reserveScrollBarGap: true
    });
  }
});

onUnmounted(() => {
  if (scrollContainer.value) {
    enableBodyScroll(scrollContainer.value);
  }
});
</script>

<style>
.app-shell {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  flex-direction: column;
  background-color: rgb(12 10 9);
  color: rgb(250 250 249);
  overflow: hidden;
  /* iOS Safari fix */
  height: 100vh;
  height: -webkit-fill-available;
}

.app-content {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.app-main {
  flex: 1;
  overflow-y: scroll;
  overflow-x: hidden;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: none;
  /* Prevent iOS bounce */
  position: relative;
}
</style>

