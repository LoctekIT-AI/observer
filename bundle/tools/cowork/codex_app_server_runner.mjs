#!/usr/bin/env node

import { spawn } from "node:child_process";
import { appendFile, mkdir, writeFile } from "node:fs/promises";
import { once } from "node:events";
import path from "node:path";
import readline from "node:readline";

function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith("--") || value === undefined) {
      throw new Error(`invalid argument at position ${index + 1}`);
    }
    options[flag.slice(2)] = value;
  }
  for (const required of ["codex", "cwd", "evidence", "skill-name"]) {
    if (!options[required]) throw new Error(`--${required} is required`);
  }
  return options;
}

async function readPrompt() {
  let prompt = "";
  process.stdin.setEncoding("utf8");
  for await (const chunk of process.stdin) prompt += chunk;
  if (!prompt.trim()) throw new Error("task prompt on stdin must be non-empty");
  return prompt;
}

function rpcError(message, method) {
  if (!message?.error) return;
  const detail = typeof message.error.message === "string"
    ? message.error.message
    : JSON.stringify(message.error);
  throw new Error(`${method} failed: ${detail}`);
}

async function run() {
  const options = parseArgs(process.argv.slice(2));
  const prompt = await readPrompt();
  const evidencePath = path.resolve(options.evidence);
  await mkdir(path.dirname(evidencePath), { recursive: true });
  await writeFile(evidencePath, "", "utf8");

  const child = spawn(options.codex, ["app-server", "--stdio"], {
    cwd: path.resolve(options.cwd),
    env: { ...process.env },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const exitPromise = once(child, "exit");
  let nativeStderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    nativeStderr += chunk;
    if (nativeStderr.length > 1_000_000) child.kill("SIGTERM");
  });

  const lines = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  const iterator = lines[Symbol.asyncIterator]();
  const send = (message) => {
    child.stdin.write(`${JSON.stringify(message)}\n`);
  };
  const nextMessage = async () => {
    const next = await iterator.next();
    if (next.done) {
      const native = nativeStderr.trim();
      throw new Error(`codex app-server stdout closed${native ? `: ${native}` : ""}`);
    }
    await appendFile(evidencePath, `${next.value}\n`, "utf8");
    try {
      return JSON.parse(next.value);
    } catch (error) {
      throw new Error(`codex app-server returned invalid JSON: ${error.message}`);
    }
  };
  const waitFor = async (predicate) => {
    while (true) {
      const message = await nextMessage();
      if (predicate(message)) return message;
    }
  };

  try {
    send({
      id: 1,
      method: "initialize",
      params: { clientInfo: { name: "aihub-observatory", version: "0.1.0" } },
    });
    const initialized = await waitFor((message) => message.id === 1);
    rpcError(initialized, "initialize");
    send({ method: "initialized", params: {} });

    send({
      id: 2,
      method: "skills/list",
      params: { cwds: [path.resolve(options.cwd)], forceReload: true },
    });
    const skillsResponse = await waitFor((message) => message.id === 2);
    rpcError(skillsResponse, "skills/list");
    const skill = (skillsResponse.result?.data || [])
      .flatMap((entry) => Array.isArray(entry.skills) ? entry.skills : [])
      .find((entry) => entry.name === options["skill-name"] && entry.enabled !== false);
    if (!skill || typeof skill.path !== "string" || !skill.path) {
      throw new Error(`enabled Codex Skill not found: ${options["skill-name"]}`);
    }

    send({
      id: 3,
      method: "thread/start",
      params: {
        cwd: path.resolve(options.cwd),
        approvalPolicy: "never",
        sandbox: "read-only",
        ephemeral: false,
      },
    });
    const threadResponse = await waitFor((message) => message.id === 3);
    rpcError(threadResponse, "thread/start");
    const threadId = threadResponse.result?.thread?.id;
    if (typeof threadId !== "string" || !threadId) throw new Error("thread/start returned no thread id");

    send({
      id: 4,
      method: "turn/start",
      params: {
        threadId,
        input: [
          { type: "skill", name: skill.name, path: skill.path },
          { type: "text", text: prompt },
        ],
      },
    });
    const turnResponse = await waitFor((message) => message.id === 4);
    rpcError(turnResponse, "turn/start");
    const turnId = turnResponse.result?.turn?.id;
    if (typeof turnId !== "string" || !turnId) throw new Error("turn/start returned no turn id");

    const terminal = await waitFor(
      (message) => message.method === "turn/completed"
        && message.params?.threadId === threadId
        && message.params?.turn?.id === turnId,
    );
    const status = terminal.params.turn.status;
    if (!new Set(["completed", "failed", "interrupted"]).has(status)) {
      throw new Error(`unsupported terminal turn status: ${status}`);
    }
    process.stdout.write(`${JSON.stringify({ threadId, turnId, status, evidenceSource: "codex_app_server" })}\n`);
  } finally {
    child.stdin.end();
    child.kill("SIGTERM");
    await exitPromise;
    lines.close();
  }
}

run().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
