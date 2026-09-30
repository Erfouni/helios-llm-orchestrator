// Drives the real MCP server over stdio, the way an MCP client does, against a
// stub gateway on loopback. No OpenRouter key, no Python agent, nothing paid.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const SERVER = fileURLToPath(new URL("../mcp/mcp-server.mjs", import.meta.url));

// Payloads deliberately differ in shape: these endpoints return free-form objects.
const PAYLOADS = {
  "/benchmarks": { version: 3, categories: { coding: { fresh: true } } },
  "/benchmarks/select": { category: "coding", model: "vendor/model", rank: 1 },
  "/benchmarks/refresh": { refreshed: false, reason: "registry is fresh" },
};

let gateway;
let client;
const requests = [];
const runBodies = [];

before(async () => {
  gateway = http.createServer((req, res) => {
    const [path, query = ""] = req.url.split("?");
    requests.push({ method: req.method, path, query });
    if (path === "/run" || path === "/compare") {
      let raw = "";
      req.on("data", (chunk) => (raw += chunk));
      req.on("end", () => {
        const body = JSON.parse(raw);
        runBodies.push({ path, body });
        const models = path === "/run" ? [body.model] : body.models;
        if (models.includes("vendor/rejected")) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "model is not allowed" }));
          return;
        }
        const answer = (model) => ({
          model_requested: model,
          model_resolved: model,
          model_used: model,
          answer: "ok",
          usage: {},
        });
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify(
            path === "/run"
              ? answer(body.model)
              : { results: body.models.map(answer) },
          ),
        );
      });
      return;
    }
    if (path === "/benchmarks/select" && !query.includes("category=coding")) {
      res.writeHead(404, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "unknown category" }));
      return;
    }
    const body = PAYLOADS[path];
    res.writeHead(body ? 200 : 404, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body ?? { error: "not found" }));
  });
  await new Promise((resolve) => gateway.listen(0, "127.0.0.1", resolve));

  client = new Client({ name: "helios-test", version: "0" });
  await client.connect(
    new StdioClientTransport({
      command: process.execPath,
      args: [SERVER],
      env: {
        ...process.env,
        HELIOS_AGENT_URL: `http://127.0.0.1:${gateway.address().port}`,
      },
    }),
  );
});

after(async () => {
  await client?.close();
  gateway?.closeAllConnections();
  await new Promise((resolve) => gateway?.close(resolve));
});

test("every tool advertises an object output schema", async () => {
  const { tools } = await client.listTools();
  assert.equal(tools.length, 6);
  for (const tool of tools) {
    assert.equal(tool.outputSchema?.type, "object", `${tool.name} has no output schema`);
  }
});

for (const [name, args, path] of [
  ["helios_get_benchmark_registry", {}, "/benchmarks"],
  ["helios_select_benchmark_model", { category: "coding" }, "/benchmarks/select"],
  ["helios_refresh_benchmarks", {}, "/benchmarks/refresh"],
]) {
  test(`${name} returns the gateway payload`, async () => {
    const result = await client.callTool({ name, arguments: args });
    assert.equal(result.isError, undefined, result.content?.[0]?.text);
    assert.deepEqual(result.structuredContent, PAYLOADS[path]);
    assert.deepEqual(JSON.parse(result.content[0].text), PAYLOADS[path]);
  });
}

test("helios_refresh_benchmarks is a POST to the refresh endpoint", async () => {
  requests.length = 0;
  await client.callTool({ name: "helios_refresh_benchmarks", arguments: {} });
  assert.deepEqual(
    requests.map((r) => [r.method, r.path]),
    [["POST", "/benchmarks/refresh"]],
  );
});

// Run and compare have strict output schemas, and the client checks
// structuredContent against them even on errors (it has listed the tools above).
for (const [name, args] of [
  ["openrouter_run_model", { model: "vendor/rejected", prompt: "hi" }],
  ["openrouter_compare_models", { models: ["vendor/a", "vendor/rejected"], prompt: "hi" }],
]) {
  test(`${name} passes a gateway rejection through as a tool error`, async () => {
    const result = await client.callTool({ name, arguments: args });
    assert.equal(result.isError, true);
    assert.equal(result.content[0].text, "model is not allowed");
  });
}

test("a gateway error comes back as a tool error, not a crash", async () => {
  const result = await client.callTool({
    name: "helios_select_benchmark_model",
    arguments: { category: "no-such-category" },
  });
  assert.equal(result.isError, true);
  assert.equal(result.content[0].text, "unknown category");
});

// The gateway rejects max_tokens above its MAX_OUTPUT_TOKENS and uses that cap
// when max_tokens is absent, so the MCP layer must not invent its own default.
for (const [name, args, path] of [
  ["openrouter_run_model", { model: "vendor/model", prompt: "hi" }, "/run"],
  [
    "openrouter_compare_models",
    { models: ["vendor/a", "vendor/b"], prompt: "hi" },
    "/compare",
  ],
]) {
  test(`${name} leaves max_tokens to the gateway when it is not given`, async () => {
    runBodies.length = 0;
    const result = await client.callTool({ name, arguments: args });
    assert.equal(result.isError, undefined, result.content?.[0]?.text);
    assert.equal(runBodies.length, 1);
    assert.equal(runBodies[0].path, path);
    assert.equal("max_tokens" in runBodies[0].body, false);
  });

  test(`${name} forwards an explicit max_tokens`, async () => {
    runBodies.length = 0;
    await client.callTool({ name, arguments: { ...args, max_tokens: 1234 } });
    assert.equal(runBodies[0].body.max_tokens, 1234);
  });
}
