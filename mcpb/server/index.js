#!/usr/bin/env node
"use strict";
/*
 * Mass Search MCP server - Node transport (Claude Desktop bundles Node, so the
 * Install button lights up), spawning the stdlib Python engine per tool call.
 * No node_modules: Node builtins only. Newline-delimited JSON-RPC 2.0 on stdio;
 * stdout is reserved for protocol frames.
 */
const readline = require("readline");
const { spawn } = require("child_process");
const path = require("path");

const SERVER_NAME = "mass-search";
const SERVER_VERSION = "0.3.0";
const PROTOCOL_DEFAULT = "2025-06-18";
const WORKER = path.join(__dirname, "worker.py");
const PYTHON = process.env.MASS_PYTHON && process.env.MASS_PYTHON.trim() ? process.env.MASS_PYTHON.trim() : "python";

const TOOLS = [
  {
    name: "web_search",
    description:
      "Fast KEYLESS web search across multiple free resolvers (DuckDuckGo, Wikipedia, " +
      "Hacker News, Crossref, GitHub, optional local SearXNG, ...). No API key, no quota - " +
      "runs on local hardware. Returns a de-duplicated hit list. Use for quick lookups.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "the search query" },
        backends: { type: "string", description: "group (web|academic|tech|all) or comma-list of resolvers", default: "web" },
        per_backend: { type: "integer", description: "results per resolver", default: 6 }
      },
      required: ["query"]
    }
  },
  {
    name: "mass_search",
    description:
      "Run a full local research CAMPAIGN: a local LLM fans the question into many diverse " +
      "queries, harvests them in parallel across keyless resolvers, DEEP-READS the top sources' " +
      "full page bodies, and SYNTHESIZES a direct, source-grounded answer (list-type goals get " +
      "the verbatim items). Keyless + local = no usage limits. Returns the answer + findings + " +
      "top sources; full corpus saved to disk (see read_campaign). Runs in the background.",
    inputSchema: {
      type: "object",
      properties: {
        question: { type: "string", description: "the research goal/question" },
        queries: { type: "integer", description: "how many queries to expand into", default: 12 },
        backends: { type: "string", description: "group or comma-list", default: "web" },
        workers: { type: "integer", description: "parallel search workers", default: 6 },
        deep: { type: "boolean", description: "read the top sources' full page bodies (not just snippets)", default: true },
        synth: { type: "boolean", description: "synthesize the final answer", default: true },
        extract: { type: "boolean", description: "LLM-distill each result set", default: true }
      },
      required: ["question"]
    }
  },
  {
    name: "read_campaign",
    description: "Reopen a saved mass_search campaign by slug. section: report (default), results, or json.",
    inputSchema: {
      type: "object",
      properties: {
        slug: { type: "string", description: "the campaign slug (shown when mass_search finished)" },
        section: { type: "string", enum: ["report", "results", "json"], default: "report" }
      },
      required: ["slug"]
    }
  }
];

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}
function result(id, r) { send({ jsonrpc: "2.0", id, result: r }); }
function rpcError(id, code, message) { send({ jsonrpc: "2.0", id, error: { code, message } }); }

function runTool(name, args) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(PYTHON, [WORKER], { env: process.env });
    } catch (e) {
      resolve({ ok: false, error: `cannot start python ('${PYTHON}'): ${e.message}` });
      return;
    }
    let out = "", err = "";
    child.on("error", (e) => resolve({ ok: false, error: `cannot start python ('${PYTHON}'): ${e.message}` }));
    child.stdout.on("data", (d) => { out += d; });
    child.stderr.on("data", (d) => { err += d; });
    child.on("close", () => {
      const line = out.trim().split("\n").filter(Boolean).pop() || "";
      try {
        resolve(JSON.parse(line));
      } catch (e) {
        resolve({ ok: false, error: `worker produced no valid result. stderr tail: ${err.slice(-600)}` });
      }
    });
    child.stdin.write(JSON.stringify({ tool: name, arguments: args }));
    child.stdin.end();
  });
}

async function handle(msg) {
  const method = msg.method;
  const id = msg.id;
  const isReq = id !== undefined && id !== null;

  if (method === "initialize") {
    const pv = (msg.params && msg.params.protocolVersion) || PROTOCOL_DEFAULT;
    result(id, {
      protocolVersion: pv,
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: SERVER_NAME, version: SERVER_VERSION }
    });
  } else if (method === "notifications/initialized" || method === "initialized") {
    // notification, no reply
  } else if (method === "ping") {
    if (isReq) result(id, {});
  } else if (method === "tools/list") {
    result(id, { tools: TOOLS });
  } else if (method === "tools/call") {
    const p = msg.params || {};
    if (!TOOLS.find((t) => t.name === p.name)) {
      rpcError(id, -32602, `unknown tool: ${p.name}`);
      return;
    }
    const res = await runTool(p.name, p.arguments || {});
    if (res && res.ok) {
      result(id, { content: [{ type: "text", text: res.text || "" }] });
    } else {
      result(id, { content: [{ type: "text", text: `error: ${(res && res.error) || "unknown"}` }], isError: true });
    }
  } else if (isReq) {
    rpcError(id, -32601, `method not found: ${method}`);
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", async (line) => {
  line = line.trim();
  if (!line) return;
  let msg;
  try { msg = JSON.parse(line); } catch (e) { return; }
  try { await handle(msg); } catch (e) { process.stderr.write(`[mass-search] handler crash: ${e}\n`); }
});
