import { ref } from "vue";
import { watchDebounced } from "@vueuse/core";
import { checkDirectory, type DirectoryCheck } from "@/api";

export function useDirectoryCheck(debounceMs = 400) {
  const input = ref("");
  const checking = ref(false);
  const probe = ref<DirectoryCheck | null>(null);
  const error = ref("");

  const check = async (path: string): Promise<DirectoryCheck | null> => {
    const trimmed = path.trim();
    if (!trimmed) {
      probe.value = null;
      error.value = "";
      checking.value = false;
      return null;
    }

    checking.value = true;
    try {
      const status = await checkDirectory(trimmed);
      probe.value = status;
      error.value = status.exists ? "" : "Directory not found";
      return status;
    } catch (err) {
      probe.value = null;
      error.value = String(err);
      return null;
    } finally {
      checking.value = false;
    }
  };

  // Auto-check when input changes (debounced)
  watchDebounced(
    input,
    (value) => {
      const trimmed = value.trim();
      if (!trimmed) {
        probe.value = null;
        error.value = "";
        checking.value = false;
        return;
      }
      checking.value = true;
      check(trimmed);
    },
    { debounce: debounceMs }
  );

  return {
    input,
    checking,
    probe,
    error,
    check
  };
}
