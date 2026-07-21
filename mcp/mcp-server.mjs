import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const LOCAL_GATEWAY = process.env.HELIOS_AGENT_URL ?? "http://127.0.0.1:3188";
const LOCAL_API_KEY = process.env.HELIOS_LOCAL_API_KEY ?? "";

async function localJson(path, options = {}) {
  const response = await fetch(LOCAL_GATEWAY + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(LOCAL_API_KEY ? { Authorization: `Bearer ${LOCAL_API_KEY}` } : {}),
      ...(options.headers ?? {}),
    },
    signal: AbortSignal.timeout(240_000),
  });
  const text = await response.text();
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("Helios gateway returned non-JSON (" + response.status + ")");
  }
  if (!response.ok) {
    throw new Error(value?.error ?? ("Helios gateway HTTP " + response.status));
  }
  return value;
}

function result(value) {
  return {
    structuredContent: value,
    content: [{ type: "text", text: JSON.stringify(value) }],
  };
}

function errorResult(error) {
  const message = error instanceof Error ? error.message : String(error);
  return {
    isError: true,
    structuredContent: { error: message },
    content: [{ type: "text", text: message }],
  };
}

const server = new McpServer(
  { name: "helios-multimodel-router", version: "1.0.0" },
  {
    instructions:
      "When the user explicitly asks to use GLM, Gemini, Claude, DeepSeek, Qwen, or another external model, call openrouter_run_model. When the user asks to compare models, call openrouter_compare_models. Include the complete relevant task or conversation context in prompt. Never claim a model was used unless model_used confirms it. Never request, read, or reveal the OpenRouter API key. External models propose content only; file writes and deletes remain separate actions requiring preview and confirmation.",
  },
);

server.registerTool(
  "openrouter_list_models",
  {
    title: "List OpenRouter models",
    description:
      "Search the live OpenRouter model catalog. Use this when a requested model name is ambiguous or the user asks what models are available.",
    inputSchema: {
      search: z.string().optional().describe("Model name, provider, or slug fragment"),
      limit: z.number().int().min(1).max(100).optional().default(25),
    },
    outputSchema: {
      models: z.array(z.object({
        id: z.string(),
        name: z.string().nullable().optional(),
        created: z.number().nullable().optional(),
        context_length: z.number().nullable().optional(),
        pricing: z.record(z.string(), z.unknown()).nullable().optional(),
        supported_parameters: z.array(z.string()).nullable().optional(),
      })),
    },
    annotations: { readOnlyHint: true },
  },
  async ({ search = "", limit = 25 }) => {
    try {
      const query = new URLSearchParams({ search, limit: String(limit) });
      return result(await localJson("/models?" + query.toString()));
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "openrouter_run_model",
  {
    title: "Run a task with an OpenRouter model",
    description:
      "MUST be used whenever the user explicitly says to use GLM, Gemini, Claude, DeepSeek, Qwen, or names an OpenRouter model. Delegates the supplied task/context to that model through the user's Mac and returns the confirmed model_used.",
    inputSchema: {
      model: z.string().describe("Friendly alias such as glm or gemini, or an exact OpenRouter model slug"),
      prompt: z.string().min(1).describe("Complete delegated task, including all relevant conversation or file context"),
      system: z.string().optional().describe("Optional system instruction for the delegated model"),
      reasoning_effort: z.enum(["low", "medium", "high", "xhigh"]).optional(),
      max_tokens: z.number().int().min(1).max(8192).optional().default(4096),
      temperature: z.number().min(0).max(2).optional(),
    },
    outputSchema: {
      model_requested: z.string(),
      model_resolved: z.string(),
      model_used: z.string(),
      answer: z.string().nullable(),
      usage: z.record(z.string(), z.unknown()),
      finish_reason: z.string().nullable().optional(),
      generation_id: z.string().nullable().optional(),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    try {
      return result(await localJson("/run", {
        method: "POST",
        body: JSON.stringify(args),
      }));
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "openrouter_compare_models",
  {
    title: "Compare answers from multiple OpenRouter models",
    description:
      "Use when the user asks to compare, cross-check, or review the same task with two or more models. Runs the models independently through the user's Mac.",
    inputSchema: {
      models: z.array(z.string()).min(2).max(4),
      prompt: z.string().min(1).describe("Complete common task and relevant context"),
      system: z.string().optional(),
      reasoning_effort: z.enum(["low", "medium", "high", "xhigh"]).optional(),
      max_tokens: z.number().int().min(1).max(8192).optional().default(4096),
      temperature: z.number().min(0).max(2).optional(),
    },
    outputSchema: {
      results: z.array(z.record(z.string(), z.unknown())),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    try {
      return result(await localJson("/compare", {
        method: "POST",
        body: JSON.stringify(args),
      }));
    } catch (error) {
      return errorResult(error);
    }
  },
);

const transport = new StdioServerTransport();
await server.connect(transport);
