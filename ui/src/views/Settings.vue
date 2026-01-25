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
</template>

<script setup lang="ts">
import { ref } from "vue";
import { clearAllData, getBaseUrl, getToken, setBaseUrl, setToken } from "../api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

const baseUrl = ref(getBaseUrl());
const token = ref(getToken());
const saved = ref(false);
const cleared = ref(false);

const save = () => {
  setBaseUrl(baseUrl.value.trim());
  setToken(token.value.trim());
  saved.value = true;
  setTimeout(() => {
    saved.value = false;
  }, 1200);
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
