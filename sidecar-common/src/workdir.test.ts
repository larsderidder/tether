/**
 * Tests for workdir.ts — working directory management.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { ensureWorkdir, setWorkdir, clearWorkdir } from "./workdir.js";
import type { SessionState } from "./types.js";
import { mkdtemp, rm, stat, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";

// Helper to create a session state
function createTestSession(id: string = "test-session"): SessionState {
  return {
    id,
    running: false,
    pendingInputs: [],
    subscribers: new Set(),
    eventBuffer: [],
  };
}

describe("ensureWorkdir", () => {
  it("returns existing workdir if set", async () => {
    const session = createTestSession();
    session.workdir = "/existing/workdir";

    const result = await ensureWorkdir(session);

    expect(result).toBe("/existing/workdir");
    expect(session.workdirManaged).toBeUndefined();
  });

  it("creates temp directory when no workdir set", async () => {
    const session = createTestSession();

    const result = await ensureWorkdir(session);

    expect(result).toBeDefined();
    expect(result).toContain(tmpdir());
    expect(result).toContain("tether_test-session_");
    expect(session.workdir).toBe(result);
    expect(session.workdirManaged).toBe(true);

    // Verify directory exists
    const stats = await stat(result);
    expect(stats.isDirectory()).toBe(true);

    // Cleanup
    await rm(result, { recursive: true, force: true });
  });

  it("returns same directory on subsequent calls", async () => {
    const session = createTestSession();

    const result1 = await ensureWorkdir(session);
    const result2 = await ensureWorkdir(session);

    expect(result1).toBe(result2);

    // Cleanup
    await rm(result1, { recursive: true, force: true });
  });
});

describe("setWorkdir", () => {
  let testDir: string;

  beforeEach(async () => {
    testDir = await mkdtemp(join(tmpdir(), "tether-test-"));
  });

  afterEach(async () => {
    await rm(testDir, { recursive: true, force: true });
  });

  it("sets workdir without managed flag", async () => {
    const session = createTestSession();

    await setWorkdir(session, testDir);

    expect(session.workdir).toBe(testDir);
    expect(session.workdirManaged).toBe(false);
  });

  it("clears previous managed workdir when setting new one", async () => {
    const session = createTestSession();

    // Create a managed temp dir
    const tempDir = await ensureWorkdir(session);
    expect(session.workdirManaged).toBe(true);

    // Set a new external workdir
    await setWorkdir(session, testDir);

    expect(session.workdir).toBe(testDir);
    expect(session.workdirManaged).toBe(false);

    // Original temp dir should be cleaned up
    await expect(stat(tempDir)).rejects.toThrow();

    // Cleanup the test dir
    await rm(testDir, { recursive: true, force: true });
  });

  it("does not clear workdir if same as current", async () => {
    const session = createTestSession();
    session.workdir = testDir;
    session.workdirManaged = true;

    await setWorkdir(session, testDir);

    // Should not change anything
    expect(session.workdir).toBe(testDir);
    expect(session.workdirManaged).toBe(false);
  });
});

describe("clearWorkdir", () => {
  it("does nothing if no workdir set", async () => {
    const session = createTestSession();

    await clearWorkdir(session);

    expect(session.workdir).toBeUndefined();
  });

  it("clears workdir without deleting if not managed", async () => {
    const session = createTestSession();
    const testDir = await mkdtemp(join(tmpdir(), "tether-test-"));

    session.workdir = testDir;
    session.workdirManaged = false;

    await clearWorkdir(session);

    expect(session.workdir).toBeUndefined();
    // Directory should still exist
    const stats = await stat(testDir);
    expect(stats.isDirectory()).toBe(true);

    // Cleanup
    await rm(testDir, { recursive: true, force: true });
  });

  it("deletes workdir if managed", async () => {
    const session = createTestSession();

    const tempDir = await ensureWorkdir(session);
    expect(session.workdirManaged).toBe(true);

    await clearWorkdir(session);

    expect(session.workdir).toBeUndefined();
    expect(session.workdirManaged).toBeUndefined();
    // Directory should be deleted
    await expect(stat(tempDir)).rejects.toThrow();
  });
});
