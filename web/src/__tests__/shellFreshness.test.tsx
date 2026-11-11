import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetFreshnessStore } from "@/lib/freshnessStore";
import { resetStatsStore } from "@/lib/statsStore";
import { AppRoutes } from "@/routes";

// R26 (owner 2026-11-08: yes), reworded by docs/specs/m5b-redesign.md RD3: the shell header carries the freshness line as a
// link to the Freshness page. It reads GET /api/freshness once per page load and words it with shellFreshness
// (lib/shellFreshness.ts: "Data as of Sep 30, 2026, 2 newer filings being added", or the plain failed and unavailable forms). The
// fixtures have the shape of the real payloads: monitor.py status_payload (:400-413) and the disabled payload of
// monitor_routes.py (:30-32). Production /api/stats.freshness has no snapshot_as_of, which is why the header does not read it.

const HEALTHY = {
  configured: true,
  enabled: true,
  status: "ok",
  checked_at: new Date().toISOString(),
  snapshot_as_of: "2026-09-30",
  last_error_at: null,
  next_check_at: new Date(Date.now() + 3600000).toISOString(),
  pending_count: 2,
  pending_filings: [],
  federal_register: null,
  unresolved: [],
  duration_s: 1.4,
};
const DISABLED = {
  checked_at: null,
  snapshot_as_of: null,
  last_error_at: null,
  next_check_at: null,
  pending_count: 0,
  pending_filings: [],
  federal_register: null,
  unresolved: [],
  duration_s: null,
};
// What production /api/stats carries under "freshness": no snapshot_as_of.
const STATS_FRESHNESS = { status: "ok", checked_at: new Date().toISOString(), pending_count: 2 };

function stubFetch(freshness: unknown | null, status = 200) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/freshness")) {
      if (freshness === null) throw new TypeError("offline");
      return new Response(JSON.stringify(freshness), { status });
    }
    if (url.includes("/api/stats")) return new Response(JSON.stringify({ freshness: STATS_FRESHNESS }), { status: 200 });
    if (url.includes("/api/examples")) return new Response(JSON.stringify({ examples: [] }), { status: 200 });
    return new Response("{}", { status: 404 });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function calls(fn: ReturnType<typeof stubFetch>, path: string): number {
  return fn.mock.calls.filter(([input]) => String(input).includes(path)).length;
}

function mount(path = "/method/", strict = false) {
  const tree = (
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>
  );
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

beforeEach(() => {
  resetStatsStore();
  resetFreshnessStore();
});
afterEach(() => vi.unstubAllGlobals());

describe("the freshness line of the shell header (R26, RD3)", () => {
  it("names the data date and the newer filings being added, in the one date style", async () => {
    stubFetch(HEALTHY);
    mount();
    await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("Data as of Sep 30, 2026, 2 newer filings being added"));
    expect(screen.getByRole("banner")).not.toHaveTextContent("2026-09-30");
  });

  it("says the data status is unavailable for the disabled payload", async () => {
    stubFetch(DISABLED);
    mount();
    await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("Data status unavailable"));
  });

  it("says in plain words that the check failed for an error status, keeping the data date", async () => {
    stubFetch({ ...HEALTHY, status: "error", last_error_at: new Date().toISOString() });
    mount();
    await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("Data as of Sep 30, 2026; the check for newer filings failed"));
  });

  it("says the data status is unavailable when /api/freshness fails or answers an error status", async () => {
    stubFetch(null);
    mount();
    await waitFor(() => expect(screen.getByRole("link", { name: /^Freshness:\s*Data status unavailable$/ })).toBeInTheDocument());
    cleanup();
    resetFreshnessStore();
    stubFetch({ detail: "Too many requests" }, 429);
    mount();
    await waitFor(() => expect(screen.getByRole("link", { name: /^Freshness:\s*Data status unavailable$/ })).toBeInTheDocument());
  });

  it("is a link to the Freshness page inside the main navigation, and never a live region", async () => {
    stubFetch(HEALTHY);
    mount();
    const nav = within(screen.getByRole("navigation", { name: "Main" }));
    const link = await nav.findByRole("link", { name: /^Freshness:\s*Data as of Sep 30, 2026/ });
    expect(link).toHaveAttribute("href", "/freshness/");
    expect(screen.getByRole("banner").querySelector("[aria-live], [role=status], output")).toBeNull();
  });

  it("has no link and no text before /api/freshness answers, and keeps its height reserved", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    mount();
    const slot = document.getElementById("shell-freshness");
    expect(slot).not.toBeNull();
    expect(slot).toHaveTextContent("");
    expect(slot?.querySelector("a")).toBeNull();
  });

  it("asks /api/freshness once for a page load, across StrictMode and every route change, and never /api/stats for it", async () => {
    const fn = stubFetch(HEALTHY);
    const user = userEvent.setup();
    mount("/method/", true);
    await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("Data as of"));
    const nav = within(screen.getByRole("navigation", { name: "Main" }));
    await user.click(nav.getByRole("link", { name: "Companies" }));
    await user.click(nav.getByRole("link", { name: "Ask" }));
    await user.click(nav.getByRole("link", { name: "Workspace" }));
    await act(async () => {});
    expect(calls(fn, "/api/freshness")).toBe(1);
  });

  it("on the Freshness page the shell and the page share the one request", async () => {
    const fn = stubFetch(HEALTHY);
    mount("/freshness/", true);
    await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("Data as of Sep 30, 2026"));
    await waitFor(() => expect(within(screen.getByRole("main")).getByRole("list", { name: "Freshness details" })).toBeInTheDocument());
    expect(calls(fn, "/api/freshness")).toBe(1);
    expect(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: /^Freshness:\s*Data as of/ })).toHaveAttribute("aria-current", "page");
  });
});
