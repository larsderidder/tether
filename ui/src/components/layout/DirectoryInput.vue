<template>
  <div class="relative">
    <Input
      :model-value="modelValue"
      @update:model-value="$emit('update:modelValue', $event)"
      placeholder="/path/to/project"
      :class="inputClass"
    />
    <div class="absolute right-3 top-1/2 -translate-y-1/2">
      <GitBranch
        v-if="probe?.exists && probe?.is_git"
        class="h-4 w-4 text-emerald-400"
        title="Git repository"
      />
      <Folder
        v-else-if="probe?.exists"
        class="h-4 w-4 text-stone-400"
        title="Directory"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { GitBranch, Folder } from "lucide-vue-next";
import { Input } from "@/components/ui/input";
import type { DirectoryCheck } from "@/api";

withDefaults(defineProps<{
  modelValue: string;
  checking?: boolean;
  probe?: DirectoryCheck | null;
  inputClass?: string;
}>(), {
  checking: false,
  probe: null,
  inputClass: "border-stone-700 bg-stone-800 pr-8"
});

defineEmits<{
  "update:modelValue": [value: string];
}>();
</script>
