import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  createSession,
  deleteSession,
  interruptSession,
  listSessions,
  syncSession,
  type Session
} from "@/api";
import { activeSessionId } from "@/state";

// Shared state (singleton pattern - same refs across all useSessions() calls)
const sessions = ref<Session[]>([]);
const loading = ref(false);
const loaded = ref(false);
const error = ref("");
const creating = ref(false);
const deleting = ref(false);
const syncing = ref(false);

export function useSessions() {
  const router = useRouter();
  const route = useRoute();

  const activeSession = computed(() =>
    sessions.value.find((s) => s.id === activeSessionId.value)
  );

  const isConnectionError = computed(() => {
    const msg = error.value.toLowerCase();
    return (
      msg.includes("failed to fetch") ||
      msg.includes("networkerror") ||
      msg.includes("500") ||
      msg.includes("502") ||
      msg.includes("503") ||
      msg.includes("connection")
    );
  });

  const maybeSelectDefaultSession = (list: Session[]) => {
    const routeId = route.params.id as string | undefined;
    if (routeId) {
      const exists = list.some((s) => s.id === routeId);
      if (exists) {
        activeSessionId.value = routeId;
        return;
      }
      router.replace({ path: "/" });
    }
    if (!activeSessionId.value && list.length) {
      activeSessionId.value = list[0].id;
    }
  };

  const refresh = async () => {
    error.value = "";
    loading.value = true;
    try {
      const fetched = await listSessions();
      sessions.value = fetched;
      maybeSelectDefaultSession(fetched);
      return fetched;
    } catch (err) {
      console.error("Failed to refresh sessions:", err);
      error.value = String(err); // Keep for isConnectionError detection
      return [];
    } finally {
      loading.value = false;
      loaded.value = true;
    }
  };

  const create = async (directory: string) => {
    creating.value = true;
    error.value = "";
    try {
      const created = await createSession({ directory });
      activeSessionId.value = created.id;
      await refresh();
      return created;
    } catch (err) {
      console.error("Failed to create session:", err);
      return null;
    } finally {
      creating.value = false;
    }
  };

  const remove = async (id: string) => {
    if (deleting.value) return;
    deleting.value = true;
    error.value = "";
    try {
      const target = sessions.value.find((s) => s.id === id);
      if (target?.state === "RUNNING") {
        await interruptSession(id);
      }
      await deleteSession(id);
      if (activeSessionId.value === id) {
        activeSessionId.value = null;
      }
      await refresh();
    } catch (err) {
      console.error("Failed to delete session:", err);
    } finally {
      deleting.value = false;
    }
  };

  const select = (id: string) => {
    activeSessionId.value = id;
  };

  const sync = async () => {
    if (!activeSessionId.value || syncing.value) return;
    if (!activeSession.value?.runner_session_id) return null;
    syncing.value = true;
    error.value = "";
    try {
      const result = await syncSession(activeSessionId.value);
      if (result.synced > 0) {
        console.log(`Synced ${result.synced} new messages`);
      }
      return result;
    } catch (err) {
      const msg = String(err);
      // 400 = not attached, 404 = external not found - both are expected
      if (!msg.includes("400") && !msg.includes("404")) {
        console.error("Failed to sync session:", err);
      }
      return null;
    } finally {
      syncing.value = false;
    }
  };

  const clearError = () => {
    error.value = "";
  };

  // Sync URL when active session changes
  watch(activeSessionId, (newId, oldId) => {
    if (newId === oldId) return;
    const routeId = route.params.id as string | undefined;
    if (newId && newId !== routeId) {
      router.replace({ name: "session", params: { id: newId } });
    } else if (!newId && routeId) {
      router.replace({ path: "/" });
    }
  });

  // Sync activeSessionId from URL (browser back/forward)
  watch(
    () => route.params.id,
    (newRouteId) => {
      const id = newRouteId as string | undefined;
      if (id && id !== activeSessionId.value) {
        activeSessionId.value = id;
      }
    }
  );

  return {
    // State (readonly where appropriate)
    sessions: computed(() => sessions.value),
    activeSessionId,
    activeSession,
    loading: computed(() => loading.value),
    loaded: computed(() => loaded.value),
    error: computed(() => error.value),
    creating: computed(() => creating.value),
    deleting: computed(() => deleting.value),
    syncing: computed(() => syncing.value),
    isConnectionError,

    // Actions
    refresh,
    create,
    remove,
    select,
    sync,
    clearError
  };
}
