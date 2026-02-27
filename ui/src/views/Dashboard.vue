<template>
  <Dialog :open="open" @update:open="handleClose">
    <DialogContent class="max-w-5xl max-h-[90vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>System Status</DialogTitle>
        <DialogDescription>
          Bridge health and session activity overview
        </DialogDescription>
      </DialogHeader>

      <div v-if="loading" class="flex items-center justify-center py-12">
        <div class="text-sm text-stone-400">Loading...</div>
      </div>

      <div v-else class="space-y-6">
        <!-- Bridge Status Section -->
        <section>
          <h3 class="text-sm font-medium text-stone-400 mb-3">Bridges</h3>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
            <BridgeStatusCard
              v-for="bridge in bridgeStatus"
              :key="bridge.platform"
              :bridge="bridge"
            />
          </div>
        </section>

        <!-- Session Statistics Section -->
        <section>
          <h3 class="text-sm font-medium text-stone-400 mb-3">Sessions</h3>
          <div class="mb-3 text-sm text-stone-500">
            Total: {{ sessionStats.total }}
          </div>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <SessionStateChart :data="sessionStats.by_state" />
            <SessionPlatformChart :data="sessionStats.by_platform" />
          </div>
        </section>

        <!-- Recent Activity Section -->
        <section>
          <h3 class="text-sm font-medium text-stone-400 mb-3">Recent Activity</h3>
          <RecentActivityList :sessions="sessionStats.recent_activity" />
        </section>
      </div>

      <DialogFooter>
        <Button variant="outline" @click="handleClose">Close</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from "vue";
import { getBridgeStatus, getSessionStats, type BridgeStatusInfo, type SessionStats } from "@/api";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import BridgeStatusCard from "@/components/dashboard/BridgeStatusCard.vue";
import SessionStateChart from "@/components/dashboard/SessionStateChart.vue";
import SessionPlatformChart from "@/components/dashboard/SessionPlatformChart.vue";
import RecentActivityList from "@/components/dashboard/RecentActivityList.vue";

const props = defineProps<{
  open: boolean;
}>();

const emit = defineEmits<{
  "update:open": [value: boolean];
}>();

const loading = ref(true);
const bridgeStatus = ref<BridgeStatusInfo[]>([]);
const sessionStats = ref<SessionStats>({
  total: 0,
  by_state: {},
  by_platform: {},
  recent_activity: [],
});

let refreshInterval: number | undefined;

async function refresh() {
  try {
    loading.value = true;
    const [bridges, stats] = await Promise.all([
      getBridgeStatus(),
      getSessionStats(),
    ]);
    bridgeStatus.value = bridges.bridges;
    sessionStats.value = stats;
  } catch (error) {
    console.error("Failed to fetch dashboard data:", error);
  } finally {
    loading.value = false;
  }
}

function handleClose() {
  emit("update:open", false);
}

// Watch for dialog opening to trigger initial load
watch(() => props.open, (isOpen) => {
  if (isOpen) {
    refresh();
    // Auto-refresh every 5 seconds when open
    refreshInterval = window.setInterval(refresh, 5000);
  } else {
    // Clear interval when closed
    if (refreshInterval !== undefined) {
      clearInterval(refreshInterval);
      refreshInterval = undefined;
    }
  }
});

onUnmounted(() => {
  if (refreshInterval !== undefined) {
    clearInterval(refreshInterval);
  }
});
</script>
