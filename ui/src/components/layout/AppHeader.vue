<template>
  <header class="shrink-0 z-20 border-b border-stone-800/40 bg-stone-950">
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
        <!-- Approval mode button -->
        <div v-if="hasActiveSession" class="relative" ref="approvalMenuRef">
          <button
            class="flex h-10 items-center gap-1.5 rounded-lg px-2.5 text-stone-400 transition hover:bg-stone-800 hover:text-stone-200"
            @click="approvalMenuOpen = !approvalMenuOpen"
            :title="approvalModeTitle"
            aria-label="Tool approval mode"
            :aria-expanded="approvalMenuOpen"
          >
            <component :is="approvalModeIcon" class="h-4 w-4" :class="approvalModeColor" />
            <span class="text-xs" :class="approvalModeColor">{{ approvalModeLabel }}</span>
          </button>

          <!-- Approval mode dropdown -->
          <transition name="fade">
            <div
              v-if="approvalMenuOpen"
              class="absolute right-0 top-full mt-1 w-56 rounded-xl border border-stone-800 bg-stone-900 py-1 shadow-xl"
            >
              <div class="px-3 py-2 text-xs font-medium text-stone-500 uppercase tracking-wide">
                Approval Mode
              </div>
              <div
                v-if="!approvalControlsSupported"
                class="mx-2 mb-1 rounded-lg border border-amber-900/50 bg-amber-950/40 px-3 py-2 text-xs text-amber-200"
              >
                {{ approvalModeUnavailableMessage }}
              </div>
              <button
                v-for="mode in approvalModes"
                :key="mode.value"
                class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition disabled:cursor-not-allowed disabled:opacity-50"
                :class="[
                  effectiveApprovalMode === mode.value ? 'text-blue-400' : 'text-stone-300',
                  { 'hover:bg-stone-800': approvalControlsSupported }
                ]"
                :disabled="!approvalControlsSupported"
                @click="handleApprovalModeChange(mode.value)"
              >
                <component :is="mode.icon" class="h-4 w-4" />
                <span class="flex-1">{{ mode.label }}</span>
                <span v-if="effectiveApprovalMode === mode.value" class="text-xs text-blue-400">✓</span>
              </button>
              <div v-if="activeSession?.approval_mode !== null" class="border-t border-stone-800 mt-1 pt-1">
                <button
                  class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-stone-400 transition disabled:cursor-not-allowed disabled:opacity-50"
                  :class="{ 'hover:bg-stone-800': approvalControlsSupported }"
                  :disabled="!approvalControlsSupported"
                  @click="handleApprovalModeChange(null)"
                >
                  <RotateCcw class="h-4 w-4" />
                  <span>Use global default</span>
                </button>
              </div>
            </div>
          </transition>
        </div>

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
import { ref, computed, onMounted, onUnmounted } from "vue";
import { Menu, MoreVertical, RefreshCw, Shield, ShieldCheck, ShieldOff, RotateCcw } from "lucide-vue-next";
import type { Session, ApprovalMode } from "@/api";
import { getApprovalMode, updateSessionApprovalMode } from "@/api";
import {
  approvalModeControlTitle,
  canChangeToolApprovalMode,
  permissionCapabilitiesForSession,
  permissionCapabilityDescription,
  permissionCapabilityLabel,
} from "@/composables/usePermissionCapabilities";

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
  "update:session": [session: Session];
}>();

const menuOpen = ref(false);
const menuRef = ref<HTMLElement | null>(null);
const approvalMenuOpen = ref(false);
const approvalMenuRef = ref<HTMLElement | null>(null);

const approvalModes = [
  { value: 0 as ApprovalMode, label: "Interactive", icon: Shield },
  { value: 1 as ApprovalMode, label: "Auto-approve edits", icon: ShieldCheck },
  { value: 2 as ApprovalMode, label: "Full auto-approve", icon: ShieldOff },
];

const approvalCapabilities = computed(() => permissionCapabilitiesForSession(props.activeSession));
const approvalControlsSupported = computed(() => canChangeToolApprovalMode(props.activeSession));
const approvalModeUnavailableMessage = computed(() => permissionCapabilityDescription(approvalCapabilities.value));

const effectiveApprovalMode = computed<ApprovalMode>(() => {
  if (props.activeSession?.approval_mode !== null && props.activeSession?.approval_mode !== undefined) {
    return props.activeSession.approval_mode as ApprovalMode;
  }
  return getApprovalMode();
});

const approvalModeIcon = computed(() => {
  if (!approvalControlsSupported.value) return ShieldOff;
  const mode = approvalModes.find((item) => item.value === effectiveApprovalMode.value);
  return mode?.icon ?? Shield;
});

const approvalModeLabel = computed(() => {
  if (!approvalControlsSupported.value) {
    return permissionCapabilityLabel(approvalCapabilities.value);
  }
  if (effectiveApprovalMode.value === 0) return "Ask";
  if (effectiveApprovalMode.value === 1) return "Edits";
  return "Auto";
});

const approvalModeColor = computed(() => {
  if (!approvalControlsSupported.value) return "text-stone-500";
  if (effectiveApprovalMode.value === 0) return "text-amber-400";
  if (effectiveApprovalMode.value === 1) return "text-blue-400";
  return "text-emerald-400";
});

const approvalModeTitle = computed(() => {
  if (!approvalControlsSupported.value) {
    return approvalModeControlTitle(props.activeSession);
  }
  const mode = approvalModes.find((item) => item.value === effectiveApprovalMode.value);
  const isOverride = props.activeSession?.approval_mode !== null;
  return `${mode?.label}${isOverride ? " (session override)" : " (global default)"}`;
});

const handleRename = () => {
  emit("rename");
  menuOpen.value = false;
};

const handleInfo = () => {
  emit("info");
  menuOpen.value = false;
};

const handleApprovalModeChange = async (mode: ApprovalMode | null) => {
  if (!props.activeSession) return;
  if (!approvalControlsSupported.value) return;
  approvalMenuOpen.value = false;
  try {
    const updated = await updateSessionApprovalMode(props.activeSession.id, mode);
    emit("update:session", updated);
  } catch (err) {
    console.error("Failed to update approval mode:", err);
  }
};

const handleClickOutside = (event: MouseEvent | TouchEvent) => {
  const target = event.target as Node;
  if (menuOpen.value && !menuRef.value?.contains(target)) {
    menuOpen.value = false;
  }
  if (approvalMenuOpen.value && !approvalMenuRef.value?.contains(target)) {
    approvalMenuOpen.value = false;
  }
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
