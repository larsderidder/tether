<template>
  <section class="space-y-5">
    <div class="space-y-2">
      <p class="text-xs uppercase tracking-[0.4em] text-stone-500">Connection</p>
      <div class="space-y-4 rounded-2xl border border-stone-800/70 bg-stone-950/40 p-4">
        <label class="space-y-2 text-sm font-medium text-stone-300">
          Base URL (optional)
          <Input v-model="baseUrl" placeholder="" class="bg-stone-950/70 text-stone-100" />
        </label>
        <label class="space-y-2 text-sm font-medium text-stone-300">
          Token
          <Input v-model="token" type="password" placeholder="" class="bg-stone-950/70 text-stone-100" />
        </label>
        <div class="flex items-center gap-3">
          <Button @click="save">Save</Button>
          <span v-if="saved" class="text-sm text-emerald-400">Saved.</span>
        </div>
      </div>
    </div>
    <div class="space-y-2">
      <p class="text-xs uppercase tracking-[0.4em] text-stone-500">Approval Mode</p>
      <div class="space-y-3 rounded-2xl border border-stone-800/70 bg-stone-950/40 p-4">
        <p class="text-sm text-stone-400">
          Default policy for new sessions when the selected adapter advertises support. Some adapters only accept an initial backend policy, and some do not support approval controls at all.
        </p>
        <p
          v-if="!settingsApprovalEnabled || !defaultAdapterCapabilities.ordinary_tool_approvals"
          class="rounded-lg border border-amber-900/50 bg-amber-950/40 px-3 py-2 text-xs text-amber-200"
        >
          {{ defaultApprovalMessage }}
        </p>
        <div class="space-y-2">
          <label class="flex items-center gap-3" :class="{ 'cursor-pointer': settingsApprovalEnabled, 'cursor-not-allowed opacity-50': !settingsApprovalEnabled }">
            <input
              type="radio"
              name="approvalMode"
              :value="0"
              v-model="approvalMode"
              :disabled="!settingsApprovalEnabled"
              @change="saveApprovalMode"
              class="w-4 h-4 accent-amber-500"
            />
            <span class="text-sm text-stone-300">
              <span class="font-medium">Interactive</span>
              <span class="text-stone-500"> Ask before tool use when the adapter can route approvals</span>
            </span>
          </label>
          <label class="flex items-center gap-3" :class="{ 'cursor-pointer': settingsApprovalEnabled, 'cursor-not-allowed opacity-50': !settingsApprovalEnabled }">
            <input
              type="radio"
              name="approvalMode"
              :value="1"
              v-model="approvalMode"
              :disabled="!settingsApprovalEnabled"
              @change="saveApprovalMode"
              class="w-4 h-4 accent-amber-500"
            />
            <span class="text-sm text-stone-300">
              <span class="font-medium">Auto-approve edits</span>
              <span class="text-stone-500"> Use the adapter's edit-friendly backend policy when available</span>
            </span>
          </label>
          <label class="flex items-center gap-3" :class="{ 'cursor-pointer': settingsApprovalEnabled, 'cursor-not-allowed opacity-50': !settingsApprovalEnabled }">
            <input
              type="radio"
              name="approvalMode"
              :value="2"
              v-model="approvalMode"
              :disabled="!settingsApprovalEnabled"
              @change="saveApprovalMode"
              class="w-4 h-4 accent-amber-500"
            />
            <span class="text-sm text-stone-300">
              <span class="font-medium">Full auto</span>
              <span class="text-stone-500"> Use the least restrictive backend policy when available</span>
            </span>
          </label>
        </div>
        <div v-if="capabilityRows.length" class="space-y-2 border-t border-stone-800 pt-3">
          <p class="text-xs uppercase tracking-[0.25em] text-stone-500">Adapter support</p>
          <div
            v-for="row in capabilityRows"
            :key="row.adapter"
            class="rounded-lg border border-stone-800 bg-stone-950/60 px-3 py-2"
          >
            <div class="flex items-center justify-between gap-3">
              <span class="text-sm font-medium text-stone-200">{{ row.adapter }}</span>
              <span class="text-xs text-stone-400">{{ row.label }}</span>
            </div>
            <p class="mt-1 text-xs text-stone-500">{{ row.description }}</p>
          </div>
        </div>
      </div>
    </div>
    <div class="space-y-2">
      <p class="text-xs uppercase tracking-[0.4em] text-stone-500">Dashboard</p>
      <div class="space-y-3 rounded-2xl border border-stone-800/70 bg-stone-950/40 p-4">
        <p class="text-sm text-stone-400">
          View system status, bridge health, and session activity.
        </p>
        <Button @click="dashboardOpen = true">Open Dashboard</Button>
      </div>
    </div>
    <div class="space-y-2">
      <p class="text-xs uppercase tracking-[0.4em] text-stone-500">Maintenance</p>
      <div class="space-y-3 rounded-2xl border border-stone-800/70 bg-stone-950/40 p-4">
        <p class="text-sm text-stone-400">
          Clear all local sessions and event logs. This cannot be undone.
        </p>
        <div class="flex items-center gap-3">
          <Button variant="destructive" @click="clearData">Clear local data</Button>
          <span v-if="cleared" class="text-sm text-emerald-400">Cleared.</span>
        </div>
      </div>
    </div>
  </section>

  <Dashboard v-model:open="dashboardOpen" />
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  clearAllData,
  getApprovalMode,
  getBaseUrl,
  getConfigCapabilities,
  getToken,
  setApprovalMode,
  setBaseUrl,
  setToken,
  type ApprovalMode,
  type ConfigCapabilities,
  type PermissionCapabilities,
} from "../api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import Dashboard from "./Dashboard.vue";
import {
  FAIL_CLOSED_PERMISSION_CAPABILITIES,
  canUseInitialApprovalPolicy,
  permissionCapabilityDescription,
  permissionCapabilityLabel,
} from "@/composables/usePermissionCapabilities";

const baseUrl = ref(getBaseUrl());
const token = ref(getToken());
const approvalMode = ref<ApprovalMode>(getApprovalMode());
const saved = ref(false);
const cleared = ref(false);
const dashboardOpen = ref(false);
const capabilities = ref<ConfigCapabilities | null>(null);

const defaultAdapterCapabilities = computed<PermissionCapabilities>(() => {
  const config = capabilities.value;
  if (!config?.default_adapter) {
    return FAIL_CLOSED_PERMISSION_CAPABILITIES;
  }
  return (
    config.permission_capabilities[config.default_adapter] ||
    FAIL_CLOSED_PERMISSION_CAPABILITIES
  );
});

const settingsApprovalEnabled = computed(() =>
  canUseInitialApprovalPolicy(defaultAdapterCapabilities.value)
);

const defaultApprovalMessage = computed(() =>
  permissionCapabilityDescription(defaultAdapterCapabilities.value)
);

const capabilityRows = computed(() => {
  const config = capabilities.value;
  if (!config) {
    return [];
  }
  return Object.values(config.permission_capabilities).map((capability) => ({
    adapter: capability.adapter,
    label: permissionCapabilityLabel(capability),
    description: permissionCapabilityDescription(capability),
  }));
});

onMounted(async () => {
  try {
    capabilities.value = await getConfigCapabilities();
  } catch (err) {
    console.error("Failed to load config capabilities:", err);
    capabilities.value = {
      default_adapter: null,
      permission_capabilities: {},
    };
  }
});

const save = () => {
  setBaseUrl(baseUrl.value.trim());
  setToken(token.value.trim());
  saved.value = true;
  setTimeout(() => {
    saved.value = false;
  }, 1200);
};

const saveApprovalMode = () => {
  if (!settingsApprovalEnabled.value) {
    return;
  }
  setApprovalMode(approvalMode.value);
};

const clearData = async () => {
  if (!confirm("Clear all local sessions and event logs? This cannot be undone.")) {
    return;
  }
  await clearAllData();
  cleared.value = true;
  setTimeout(() => {
    cleared.value = false;
  }, 1200);
  window.location.reload();
};
</script>
