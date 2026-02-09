<template>
  <div class="rounded-xl border border-stone-800/70 bg-stone-950/40 p-4">
    <div v-if="sessions.length === 0" class="text-sm text-stone-500 text-center py-8">
      No recent activity
    </div>
    <div v-else class="space-y-2">
      <div
        v-for="session in sessions"
        :key="session.session_id"
        class="flex items-center justify-between p-3 rounded-lg bg-stone-900/50 hover:bg-stone-900/70 transition-colors"
      >
        <div class="flex-1 min-w-0">
          <p class="text-sm font-medium text-stone-200 truncate">
            {{ session.name }}
          </p>
          <div class="flex items-center gap-3 mt-1">
            <span
              :class="[
                'inline-flex items-center px-2 py-0.5 rounded text-xs font-medium',
                stateColor(session.state)
              ]"
            >
              {{ session.state }}
            </span>
            <span v-if="session.platform" class="text-xs text-stone-500 capitalize">
              {{ session.platform }}
            </span>
            <span class="text-xs text-stone-500">
              {{ formatRelativeTime(session.last_activity_at) }}
            </span>
          </div>
        </div>
        <div class="flex-shrink-0 text-right">
          <p class="text-sm text-stone-400">
            {{ session.message_count }} msg{{ session.message_count !== 1 ? 's' : '' }}
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { SessionActivityInfo } from "@/api";

defineProps<{
  sessions: SessionActivityInfo[];
}>();

function stateColor(state: string): string {
  switch (state) {
    case "CREATED":
      return "bg-stone-700/50 text-stone-300";
    case "RUNNING":
      return "bg-blue-500/20 text-blue-400";
    case "AWAITING_INPUT":
      return "bg-amber-500/20 text-amber-400";
    case "INTERRUPTING":
      return "bg-orange-500/20 text-orange-400";
    case "ERROR":
      return "bg-red-500/20 text-red-400";
    default:
      return "bg-stone-700/50 text-stone-300";
  }
}

function formatRelativeTime(timestamp: string): string {
  try {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHour = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHour / 24);

    if (diffSec < 60) return "just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    if (diffHour < 24) return `${diffHour}h ago`;
    if (diffDay < 7) return `${diffDay}d ago`;
    return date.toLocaleDateString();
  } catch {
    return timestamp;
  }
}
</script>
