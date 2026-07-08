import { execFile } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

type AttachOptions = {
  platform?: string;
  name?: string;
};

type ExecResult = {
  stdout: string;
  stderr: string;
};

const VALID_PLATFORMS = new Set(["auto", "none", "telegram", "slack", "discord"]);

export default function (pi: ExtensionAPI) {
  pi.registerCommand("tether", {
    description: "Attach this pi session to Tether",
    handler: async (args, ctx) => {
      try {
        const result = await attachCurrentSession(ctx, {
          platform: normalizePlatform(args),
          name: pi.getSessionName(),
        });
        ctx.ui.notify(result, "info");
      } catch (error) {
        ctx.ui.notify(formatError(error), "error");
      }
    },
  });

  pi.registerTool({
    name: "tether_attach",
    label: "Attach to Tether",
    description: "Attach the current pi session to Tether.",
    promptSnippet: "Attach the current pi session to Tether when the user asks for remote supervision.",
    promptGuidelines: [
      "Use tether_attach only when the user asks to attach this pi session to Tether.",
    ],
    parameters: Type.Object({
      platform: Type.Optional(
        Type.Union([
          Type.Literal("auto"),
          Type.Literal("none"),
          Type.Literal("telegram"),
          Type.Literal("slack"),
          Type.Literal("discord"),
        ], { description: "Bridge platform to bind, or auto to use the single running bridge." }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      try {
        const message = await attachCurrentSession(ctx, {
          ...params,
          name: pi.getSessionName(),
        });
        return { content: [{ type: "text", text: message }] };
      } catch (error) {
        return { content: [{ type: "text", text: formatError(error) }], isError: true };
      }
    },
  });
}

async function attachCurrentSession(
  ctx: ExtensionContext | ExtensionCommandContext,
  options: AttachOptions,
): Promise<string> {
  flushCurrentSessionHeader(ctx);
  const sessionId = getCurrentSessionId(ctx);
  const args = ["attach-current", "--runner-type", "pi", "--directory", ctx.cwd, "--json"];

  if (sessionId) {
    args.push("--external-id", sessionId);
  }

  if (options.name) {
    args.push("--name", options.name);
  }

  const platform = options.platform || process.env.TETHER_ATTACH_BRIDGE || "auto";
  if (platform) {
    args.push("--bridge", platform);
  }

  const tetherBin = process.env.TETHER_BIN || "tether";
  const result = await execFileAsync(tetherBin, args, { timeoutMs: 30000 });
  const parsed = parseJson(result.stdout);
  if (parsed && typeof parsed.session_id === "string") {
    const bridge = parsed.platform ? ` on ${parsed.platform}` : "";
    const name = typeof parsed.name === "string" && parsed.name ? ` as ${parsed.name}` : "";
    return `Attached to Tether session ${parsed.session_id}${bridge}${name}.`;
  }

  const text = result.stdout.trim() || result.stderr.trim();
  return text || "Attached to Tether.";
}

function normalizePlatform(args: string): string {
  const value = args.trim().split(/\s+/).filter(Boolean)[0] || "auto";
  if (!VALID_PLATFORMS.has(value)) {
    throw new Error("Usage: /tether [auto|none|telegram|slack|discord]");
  }
  return value;
}

function getCurrentSessionId(ctx: ExtensionContext | ExtensionCommandContext): string | undefined {
  const sessionId = ctx.sessionManager.getSessionId();
  if (sessionId) {
    return sessionId;
  }

  const sessionFile = ctx.sessionManager.getSessionFile();
  return sessionFile ? readPiSessionId(sessionFile) : undefined;
}

function flushCurrentSessionHeader(ctx: ExtensionContext | ExtensionCommandContext): void {
  const sessionFile = ctx.sessionManager.getSessionFile();
  if (!sessionFile || existsSync(sessionFile)) {
    return;
  }

  const manager = ctx.sessionManager as unknown as {
    _rewriteFile?: () => void;
    flushed?: boolean;
    isPersisted?: () => boolean;
  };
  if (manager.isPersisted?.() === false || typeof manager._rewriteFile !== "function") {
    return;
  }

  manager._rewriteFile();
  manager.flushed = true;
}

function readPiSessionId(sessionFile: string): string | undefined {
  try {
    const lines = readFileSync(sessionFile, "utf8").split(/\r?\n/);
    for (const line of lines) {
      if (!line.trim()) continue;
      const record = JSON.parse(line);
      if (record?.type === "session" && typeof record.id === "string" && record.id) {
        return record.id;
      }
    }
  } catch {
    return undefined;
  }
  return undefined;
}

function execFileAsync(
  file: string,
  args: string[],
  options: { timeoutMs: number },
): Promise<ExecResult> {
  return new Promise((resolve, reject) => {
    execFile(file, args, { timeout: options.timeoutMs }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(stderr.trim() || error.message));
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

function parseJson(text: string): Record<string, unknown> | undefined {
  try {
    return JSON.parse(text) as Record<string, unknown>;
  } catch {
    return undefined;
  }
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
