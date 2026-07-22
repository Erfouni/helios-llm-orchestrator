import { spawnSync } from "node:child_process";

const args = process.argv.slice(2);
if (!args.length) {
  console.error("Usage: node scripts/run-python.mjs <python arguments>");
  process.exit(2);
}

const candidates = [];
if (process.env.HELIOS_PYTHON_BIN) {
  candidates.push([process.env.HELIOS_PYTHON_BIN, []]);
}
if (process.platform === "win32") {
  candidates.push(["py", ["-3"]], ["python", []]);
} else {
  candidates.push(["python3", []], ["python", []]);
}

for (const [command, prefix] of candidates) {
  const result = spawnSync(command, [...prefix, ...args], { stdio: "inherit" });
  if (!result.error || result.error.code !== "ENOENT") {
    process.exit(result.status ?? 1);
  }
}

console.error("Python 3.10+ was not found.");
process.exit(1);
