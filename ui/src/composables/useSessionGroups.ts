import { computed, ref, type Ref, type ComputedRef } from "vue";
import type { Session } from "@/api";

export type DirectoryGroup = {
  key: string;
  label: string;
  path: string | null;
  sessions: Session[];
  hasGit: boolean;
};

function formatDirectoryLabel(dir: string | null): string {
  if (!dir) {
    return "Temporary workspace";
  }
  const trimmed = dir.replace(/[\\/]+$/, "");
  const segments = trimmed.split(/[\\/]/).filter(Boolean);
  return segments.at(-1) || trimmed;
}

export function useSessionGroups(
  sessions: Ref<Session[]> | ComputedRef<Session[]>
) {
  const searchQuery = ref("");
  const expandedDirectories = ref(new Set<string>());

  const groups = computed<DirectoryGroup[]>(() => {
    const map = new Map<string, DirectoryGroup>();

    sessions.value.forEach((session) => {
      const key = session.directory ?? session.id;
      if (!map.has(key)) {
        map.set(key, {
          key,
          label: formatDirectoryLabel(session.directory),
          path: session.directory,
          sessions: [],
          hasGit: Boolean(session.directory_has_git)
        });
      }
      const group = map.get(key)!;
      group.sessions.push(session);
      if (session.directory_has_git) {
        group.hasGit = true;
      }
    });

    // Sort sessions within each group by last_activity_at descending
    const result = Array.from(map.values());
    for (const group of result) {
      group.sessions.sort((a, b) => {
        const aTime = a.last_activity_at || a.created_at || "";
        const bTime = b.last_activity_at || b.created_at || "";
        return bTime.localeCompare(aTime);
      });
    }

    // Sort groups by their most recent session's activity descending
    result.sort((a, b) => {
      const aLatest = a.sessions[0]?.last_activity_at || a.sessions[0]?.created_at || "";
      const bLatest = b.sessions[0]?.last_activity_at || b.sessions[0]?.created_at || "";
      return bLatest.localeCompare(aLatest);
    });

    return result;
  });

  const filteredGroups = computed<DirectoryGroup[]>(() => {
    const query = searchQuery.value.trim().toLowerCase();
    if (!query) {
      return groups.value;
    }

    return groups.value
      .map((group) => {
        const labelMatch = group.label.toLowerCase().includes(query);
        const pathMatch = group.path?.toLowerCase().includes(query);

        const matchingSessions = group.sessions.filter((session) => {
          const idMatch = (session.runner_session_id || session.id)
            .toLowerCase()
            .includes(query);
          const nameMatch = session.name?.toLowerCase().includes(query);
          return idMatch || nameMatch;
        });

        if (labelMatch || pathMatch) {
          return group;
        }
        if (matchingSessions.length > 0) {
          return { ...group, sessions: matchingSessions };
        }
        return null;
      })
      .filter((g): g is DirectoryGroup => g !== null);
  });

  const isExpanded = (key: string) => {
    return expandedDirectories.value.has(key);
  };

  const toggle = (key: string) => {
    if (expandedDirectories.value.has(key)) {
      expandedDirectories.value.delete(key);
    } else {
      expandedDirectories.value.add(key);
    }
  };

  const expand = (key: string) => {
    expandedDirectories.value.add(key);
  };

  const expandAll = () => {
    groups.value.forEach((g) => {
      expandedDirectories.value.add(g.key);
    });
  };

  const collapseAll = () => {
    expandedDirectories.value.clear();
  };

  const expandForSession = (session: Session | undefined) => {
    if (!session) return;
    const key = session.directory ?? session.id;
    expandedDirectories.value.add(key);
  };

  return {
    searchQuery,
    groups,
    filteredGroups,
    isExpanded,
    toggle,
    expand,
    expandAll,
    collapseAll,
    expandForSession
  };
}
