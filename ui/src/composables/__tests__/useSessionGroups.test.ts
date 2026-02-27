import { describe, it, expect } from 'vitest';
import { ref } from 'vue';
import { useSessionGroups } from '../useSessionGroups';
import type { Session } from '@/api';

const createSession = (overrides: Partial<Session> = {}): Session => ({
  id: 'session-1',
  state: 'CREATED',
  name: null,
  created_at: '2024-01-01T00:00:00Z',
  started_at: null,
  ended_at: null,
  last_activity_at: '2024-01-01T00:00:00Z',
  exit_code: null,
  summary: null,
  runner_header: null,
  runner_type: null,
  runner_session_id: null,
  directory: null,
  directory_has_git: false,
  message_count: 0,
  ...overrides,
});

describe('useSessionGroups', () => {
  describe('groups', () => {
    it('groups sessions by directory', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/project-a' }),
        createSession({ id: '2', directory: '/project-a' }),
        createSession({ id: '3', directory: '/project-b' }),
      ]);

      const { groups } = useSessionGroups(sessions);

      expect(groups.value).toHaveLength(2);
      expect(groups.value[0].path).toBe('/project-a');
      expect(groups.value[0].sessions).toHaveLength(2);
      expect(groups.value[1].path).toBe('/project-b');
      expect(groups.value[1].sessions).toHaveLength(1);
    });

    it('uses session id as key for sessions without directory', () => {
      const sessions = ref([
        createSession({ id: 'orphan-1', directory: null }),
        createSession({ id: 'orphan-2', directory: null }),
      ]);

      const { groups } = useSessionGroups(sessions);

      // Each orphan session gets its own group
      expect(groups.value).toHaveLength(2);
    });

    it('extracts directory label from path', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/home/user/projects/my-app' }),
      ]);

      const { groups } = useSessionGroups(sessions);

      expect(groups.value[0].label).toBe('my-app');
    });

    it('sets hasGit flag if any session has git', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/project', directory_has_git: false }),
        createSession({ id: '2', directory: '/project', directory_has_git: true }),
      ]);

      const { groups } = useSessionGroups(sessions);

      expect(groups.value[0].hasGit).toBe(true);
    });
  });

  describe('filteredGroups', () => {
    it('returns all groups when search is empty', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/project-a' }),
        createSession({ id: '2', directory: '/project-b' }),
      ]);

      const { searchQuery, filteredGroups } = useSessionGroups(sessions);
      searchQuery.value = '';

      expect(filteredGroups.value).toHaveLength(2);
    });

    it('filters by directory label', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/home/user/frontend' }),
        createSession({ id: '2', directory: '/home/user/backend' }),
      ]);

      const { searchQuery, filteredGroups } = useSessionGroups(sessions);
      searchQuery.value = 'front';

      expect(filteredGroups.value).toHaveLength(1);
      expect(filteredGroups.value[0].label).toBe('frontend');
    });

    it('filters by directory path', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/home/alice/project' }),
        createSession({ id: '2', directory: '/home/bob/project' }),
      ]);

      const { searchQuery, filteredGroups } = useSessionGroups(sessions);
      searchQuery.value = 'alice';

      expect(filteredGroups.value).toHaveLength(1);
    });

    it('filters by session name', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/project', name: 'Fix login bug' }),
        createSession({ id: '2', directory: '/project', name: 'Add feature' }),
      ]);

      const { searchQuery, filteredGroups } = useSessionGroups(sessions);
      searchQuery.value = 'login';

      expect(filteredGroups.value).toHaveLength(1);
      expect(filteredGroups.value[0].sessions).toHaveLength(1);
      expect(filteredGroups.value[0].sessions[0].name).toBe('Fix login bug');
    });

    it('filters by session id', () => {
      const sessions = ref([
        createSession({ id: 'abc123', directory: '/project' }),
        createSession({ id: 'xyz789', directory: '/project' }),
      ]);

      const { searchQuery, filteredGroups } = useSessionGroups(sessions);
      searchQuery.value = 'abc';

      expect(filteredGroups.value).toHaveLength(1);
      expect(filteredGroups.value[0].sessions).toHaveLength(1);
    });

    it('is case insensitive', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/MyProject' }),
      ]);

      const { searchQuery, filteredGroups } = useSessionGroups(sessions);
      searchQuery.value = 'myproject';

      expect(filteredGroups.value).toHaveLength(1);
    });
  });

  describe('expand/collapse', () => {
    it('tracks expanded state', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/project' }),
      ]);

      const { isExpanded, toggle } = useSessionGroups(sessions);

      expect(isExpanded('/project')).toBe(false);
      toggle('/project');
      expect(isExpanded('/project')).toBe(true);
      toggle('/project');
      expect(isExpanded('/project')).toBe(false);
    });

    it('expands all directories', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/project-a' }),
        createSession({ id: '2', directory: '/project-b' }),
      ]);

      const { isExpanded, expandAll } = useSessionGroups(sessions);

      expandAll();

      expect(isExpanded('/project-a')).toBe(true);
      expect(isExpanded('/project-b')).toBe(true);
    });

    it('collapses all directories', () => {
      const sessions = ref([
        createSession({ id: '1', directory: '/project-a' }),
        createSession({ id: '2', directory: '/project-b' }),
      ]);

      const { isExpanded, expandAll, collapseAll } = useSessionGroups(sessions);

      expandAll();
      collapseAll();

      expect(isExpanded('/project-a')).toBe(false);
      expect(isExpanded('/project-b')).toBe(false);
    });

    it('expands directory for a specific session', () => {
      const session = createSession({ id: '1', directory: '/project' });
      const sessions = ref([session]);

      const { isExpanded, expandForSession } = useSessionGroups(sessions);

      expandForSession(session);

      expect(isExpanded('/project')).toBe(true);
    });
  });
});
