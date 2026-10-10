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
  "/route": { category: "coding", confidence: 0.9, needs_confirmation: false, selection: null },
  "/decide": { answers: { done: { type: "noul", noul: 0.97 } }, usage: { cost: 0.00003 } },
  "/v2/global-context": { version: 4, context: { language: "fa" } },
  "/manus/tasks/task-1": { provider: "manus", task_id: "task-1", status: "running" },
};

const GATEWAY_MAX_OUTPUT_TOKENS = 16000;

let gateway;
let client;
const requests = [];
const runBodies = [];

before(async () => {
  gateway = http.createServer((req, res) => {
    const [path, query = ""] = req.url.split("?");
    requests.push({ method: req.method, path, query });
    if (path.startsWith('/v2/executions/')) {
      res.writeHead(200, {'Content-Type':'application/json'});
      res.end(JSON.stringify({billing_status:'known'}));
      return;
    }
    if (path.startsWith('/v2/tasks/') && ['enqueue', 'verify'].includes(path.split('/').at(-1))) {
      let raw = '';
      req.on('data', chunk => raw += chunk);
      req.on('end', () => {
        runBodies.push({path, body: JSON.parse(raw)});
        res.writeHead(200, {'Content-Type':'application/json'});
        res.end(JSON.stringify({status: path.endsWith('enqueue') ? 'queued' : 'succeeded'}));
      });
      return;
    }
    if (path === "/route" || path === "/decide") {
      let raw = "";
      req.on("data", (chunk) => (raw += chunk));
      req.on("end", () => {
        runBodies.push({ path, body: JSON.parse(raw) });
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(PAYLOADS[path]));
      });
      return;
    }
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
        // Stand-in for an operator who raised MAX_OUTPUT_TOKENS on the gateway.
        if (body.max_tokens > GATEWAY_MAX_OUTPUT_TOKENS) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              error: `max_tokens must be between 1 and ${GATEWAY_MAX_OUTPUT_TOKENS}`,
            }),
          );
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
  assert.equal(tools.length, 22);
  for (const tool of tools) {
    assert.equal(tool.outputSchema?.type, "object", `${tool.name} has no output schema`);
  }
});

for (const [name, args, suffix] of [
  ['helios_get_execution', {}, ''],
  ['helios_reconcile_execution', {idempotency_key:'settle',cost_usd:0.01,tokens:20,billing_evidence:{source:'statement',reference:'gen-1',details:'verified final bill'}}, '/reconcile'],
]) {
  test(`${name} exposes execution billing recovery`, async () => {
    requests.length=0;
    const execution_id='22222222-2222-4222-8222-222222222222';
    const reply=await client.callTool({name,arguments:{execution_id,...args}});
    assert.equal(reply.isError,undefined,reply.content?.[0]?.text);
    assert.equal(reply.structuredContent.billing_status,'known');
    assert.equal(requests[0].path,`/v2/executions/${execution_id}${suffix}`);
  });
}

test('durable execution can enqueue with evaluated Max settings', async () => {
  runBodies.length = 0;
  const task_id = '11111111-1111-4111-8111-111111111111';
  const result = await client.callTool({name:'helios_run_task', arguments:{task_id,version:1,idempotency_key:'queue-1',model:'test/model',max_tokens:100,reasoning_effort:'max',temperature:0.2,top_p:0.8,enqueue:true}});
  assert.equal(result.isError,undefined,result.content?.[0]?.text);
  assert.equal(result.structuredContent.status,'queued');
  assert.equal(runBodies[0].path,`/v2/tasks/${task_id}/enqueue`);
  assert.equal(runBodies[0].body.reasoning_effort,'max');
  assert.equal(runBodies[0].body.temperature,0.2);
  assert.equal(runBodies[0].body.top_p,0.8);
});

test('benchmark routing forwards execution requirements on both interfaces', async () => {
  const requirements={max_tokens:100,requested_parameters:{temperature:0.2},input_tokens:300,max_cost_usd:0.01};
  requests.length=0;
  let reply=await client.callTool({name:'helios_select_benchmark_model',arguments:{category:'coding',requirements}});
  assert.equal(reply.isError,undefined,reply.content?.[0]?.text);
  assert.deepEqual(JSON.parse(new URLSearchParams(requests[0].query).get('requirements')),requirements);
  runBodies.length=0;
  reply=await client.callTool({name:'helios_route_task',arguments:{task:'Fix bug',requirements}});
  assert.equal(reply.isError,undefined,reply.content?.[0]?.text);
  assert.deepEqual(runBodies[0].body.requirements,requirements);
});

test('task verification forwards artifact-bound evidence object', async () => {
  runBodies.length = 0;
  const evidence = {artifact_id:'artifact-1',checksum_sha256:'a'.repeat(64),checks:[{criterion:'works',passed:true,details:'test passed'}],host_check:{command:'npm test',exit_code:0,output:'passed'}};
  const result = await client.callTool({name:'helios_review_task',arguments:{task_id:'11111111-1111-4111-8111-111111111111',action:'verify',version:3,idempotency_key:'verify-1',decision:'pass',evidence}});
  assert.equal(result.isError,undefined,result.content?.[0]?.text);
  assert.deepEqual(runBodies[0].body.evidence,evidence);
});

for (const [name, args, path] of [
  ["helios_get_benchmark_registry", {}, "/benchmarks"],
  ["helios_select_benchmark_model", { category: "coding" }, "/benchmarks/select"],
  ["helios_refresh_benchmarks", {}, "/benchmarks/refresh"],
  ["helios_get_global_context", {}, "/v2/global-context"],
  ["manus_get_task", { task_id: "task-1" }, "/manus/tasks/task-1"],
]) {
  test(`${name} returns the gateway payload`, async () => {
    const result = await client.callTool({ name, arguments: args });
    assert.equal(result.isError, undefined, result.content?.[0]?.text);
    assert.deepEqual(result.structuredContent, PAYLOADS[path]);
    assert.deepEqual(JSON.parse(result.content[0].text), PAYLOADS[path]);
  });
}

const DECIDE_ARGS = {
  state: "The login API now returns 401 for expired tokens.",
  questions: {
    done: { type: "noul", instructions: "Does it return 401 for expired tokens?" },
    quality: { type: "score", instructions: "How complete is it?", criteria: ["poor", "fair", "good"] },
  },
};

for (const [name, args, path] of [
  ["helios_route_task", { task: "Fix the login API" }, "/route"],
  ["helios_decide", DECIDE_ARGS, "/decide"],
]) {
  test(`${name} posts its arguments to ${path} and returns the payload`, async () => {
    runBodies.length = 0;
    const result = await client.callTool({ name, arguments: args });
    assert.equal(result.isError, undefined, result.content?.[0]?.text);
    assert.deepEqual(result.structuredContent, PAYLOADS[path]);
    assert.deepEqual(runBodies, [{ path, body: args }]);
  });
}

test("helios_decide rejects an unknown question type before the gateway", async () => {
  runBodies.length = 0;
  const result = await client.callTool({
    name: "helios_decide",
    arguments: { state: "x", questions: { q: { type: "essay", instructions: "?" } } },
  });
  assert.equal(result.isError, true);
  assert.deepEqual(runBodies, []);
});

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

  test(`${name} lets the gateway allow more than 8192 tokens`, async () => {
    runBodies.length = 0;
    const result = await client.callTool({ name, arguments: { ...args, max_tokens: 12000 } });
    assert.equal(result.isError, undefined, result.content?.[0]?.text);
    assert.equal(runBodies.length, 1);
    assert.equal(runBodies[0].body.max_tokens, 12000);
  });

  test(`${name} reports the gateway's own max_tokens limit`, async () => {
    const result = await client.callTool({ name, arguments: { ...args, max_tokens: 20000 } });
    assert.equal(result.isError, true);
    assert.equal(result.content[0].text, "max_tokens must be between 1 and 16000");
  });
}
