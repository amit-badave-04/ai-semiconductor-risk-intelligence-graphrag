/// <reference types="node" />
// Test-only access to the byte fixtures of tests/data/sse (recorded by tests/test_sse_fixtures.py). The tests read the bytes
// and the Python-written sidecar of decoded events, so the client is compared with an independent path.
import { readFileSync } from "node:fs";
import path from "node:path";

const DIR = path.resolve(import.meta.dirname, "../../../../tests/data/sse");

export const ASK_FIXTURES = [
  "cached_done",
  "live_hybrid",
  "agent_steps",
  "escalated",
  "workspace_done",
  "error_before_delta",
  "error_after_delta",
  "ping_interleaved",
  "done_max_size",
] as const;
export const JOB_FIXTURES = ["job_ready", "job_failed"] as const;

export function fixtureBytes(name: string): Uint8Array {
  return new Uint8Array(readFileSync(path.join(DIR, `${name}.sse`)));
}

export function fixtureEvents(name: string): Record<string, unknown>[] {
  const raw = readFileSync(path.join(DIR, `${name}.expected.json`), "utf-8");
  return (JSON.parse(raw) as { events: Record<string, unknown>[] }).events;
}

export interface Refusal {
  name: string;
  status: number;
  detail: string;
  retry_after: string | null;
}

export function fixtureRefusals(): Refusal[] {
  const raw = readFileSync(path.join(DIR, "refusals.json"), "utf-8");
  return (JSON.parse(raw) as { refusals: Refusal[] }).refusals;
}

export function contractDoc(): string {
  return readFileSync(path.resolve(import.meta.dirname, "../../../../docs/v2/M5A_SSE_CONTRACT.md"), "utf-8");
}

export interface RecordedScenario {
  name: string;
  events: Record<string, unknown>[];
}

/** The events of the pre-M5 recordings of the agent and workspace paths (tests/data/*_events_pre_m5.json): real shapes the
 *  guards must accept. The recorder's "<elapsed_s>" placeholder stands for a float and is replaced by one. */
export function preM5Events(): RecordedScenario[] {
  const dir = path.resolve(import.meta.dirname, "../../../../tests/data");
  return ["agent_events_pre_m5.json", "workspace_events_pre_m5.json"].flatMap((file) => {
    const doc = JSON.parse(readFileSync(path.join(dir, file), "utf-8").replaceAll('"<elapsed_s>"', "0.5")) as {
      scenarios: RecordedScenario[];
    };
    return doc.scenarios.map(({ name, events }) => ({ name, events }));
  });
}
