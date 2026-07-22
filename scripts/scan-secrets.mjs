import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const patterns = [
  /sk-(?:or-v1-|proj-|svcacct-|ant-)?[A-Za-z0-9_-]{20,}/g,
  /gh[pousr]_[A-Za-z0-9_]{20,}/g,
  /github_pat_[A-Za-z0-9_]{20,}/g,
  /glpat-[A-Za-z0-9_-]{20,}/g,
  /xox[baprs]-[A-Za-z0-9-]{20,}/g,
  /A(?:KIA|SIA)[0-9A-Z]{16}/g,
  /AIza[0-9A-Za-z_-]{35}/g,
  /(?:sk|rk)_live_[A-Za-z0-9]{20,}/g,
  /WPL_AP1\.[A-Za-z0-9._=-]{12,}/g,
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g,
];

const listed = execFileSync(
  "git",
  ["ls-files", "-co", "--exclude-standard", "-z"],
  { encoding: "utf8" },
);
const files = listed.split("\0").filter(Boolean);
const findings = [];

for (const file of files) {
  if (file === "scripts/scan-secrets.mjs" || file === "scripts/scan-secrets.sh") {
    continue;
  }
  let bytes;
  try {
    bytes = readFileSync(file);
  } catch {
    continue;
  }
  if (bytes.includes(0)) {
    continue;
  }
  const value = bytes.toString("utf8");
  for (const pattern of patterns) {
    pattern.lastIndex = 0;
    for (const match of value.matchAll(pattern)) {
      const line = value.slice(0, match.index).split("\n").length;
      findings.push(file + ":" + line);
    }
  }
}

if (findings.length) {
  console.error("Potential secret detected in:");
  for (const location of [...new Set(findings)]) {
    console.error("  " + location);
  }
  process.exit(1);
}

console.log("Secret scan passed.");
