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
          Control when Claude needs your permission to use tools.
        </p>
        <div class="space-y-2">
          <label class="flex items-center gap-3 cursor-pointer">
            <input
              type="radio"
              name="approvalMode"
              :value="0"
              v-model="approvalMode"
              @change="saveApprovalMode"
              class="w-4 h-4 accent-amber-500"
            />
            <span class="text-sm text-stone-300">
              <span class="font-medium">Interactive</span>
              <span class="text-stone-500"> — Ask before each tool use</span>
            </span>
          </label>
          <label class="flex items-center gap-3 cursor-pointer">
            <input
              type="radio"
              name="approvalMode"
              :value="1"
              v-model="approvalMode"
              @change="saveApprovalMode"
              class="w-4 h-4 accent-amber-500"
            />
            <span class="text-sm text-stone-300">
              <span class="font-medium">Auto-approve edits</span>
              <span class="text-stone-500"> — Only ask for destructive actions</span>
            </span>
          </label>
          <label class="flex items-center gap-3 cursor-pointer">
            <input
              type="radio"
              name="approvalMode"
              :value="2"
              v-model="approvalMode"
              @change="saveApprovalMode"
              class="w-4 h-4 accent-amber-500"
            />
            <span class="text-sm text-stone-300">
              <span class="font-medium">Full auto</span>
              <span class="text-stone-500"> — Never ask (current default)</span>
            </span>
          </label>
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
import { ref } from "vue";
import {
  clearAllData,
  getApprovalMode,
  getBaseUrl,
  getToken,
  setApprovalMode,
  setBaseUrl,
  setToken,
  type ApprovalMode,
} from "../api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import Dashboard from "./Dashboard.vue";

const baseUrl = ref(getBaseUrl());
const token = ref(getToken());
const approvalMode = ref<ApprovalMode>(getApprovalMode());
const saved = ref(false);
const cleared = ref(false);
const dashboardOpen = ref(false);

const save = () => {
  setBaseUrl(baseUrl.value.trim());
  setToken(token.value.trim());
  saved.value = true;
  setTimeout(() => {
    saved.value = false;
  }, 1200);
};

const saveApprovalMode = () => {
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
