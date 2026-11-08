import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetFreshnessStore } from "@/lib/freshnessStore";
import { resetStatsStore } from "@/lib/statsStore";
import { AppRoutes } from "@/routes";

// R26 (owner 2026-11-08: yes): the shell header carries the freshness line the way the legacy header does: it reads
// GET /api/freshness (index.html:639) once per page load and words it with freshnessLine (pages/Freshness.lib.ts). The
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

describe("the freshness line of the shell header (R26)", () => {
  it("words a healthy production payload as the legacy header does, with the data date", async () => {
    stubFetch(HEALTHY);
    mount();
    await waitFor(() =>
      expect(screen.getByRole("banner")).toHaveTextContent("Data as of 2026-09-30 \u00b7 checked just now \u00b7 2 filings pending"),
    );
    expect(screen.getByRole("banner")).not.toHaveTextContent("Data freshness unknown");
  });

  it("says 'freshness check unavailable' for the disabled payload", async () => {
    stubFetch(DISABLED);
    mount();
    await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("freshness check unavailable"));
  });

  it("says the check failed for an error status", async () => {
    stubFetch({ ...HEALTHY, status: "error", last_error_at: new Date().toISOString() });
    mount();
    await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("freshness check failed just now"));
  });

  it("says 'freshness check unavailable' (the legacy words) when /api/freshness fails or answers an error status", async () => {
    stubFetch(null);
    mount();
    await waitFor(() => expect(document.getElementById("shell-freshness")).toHaveTextContent("freshness check unavailable"));
    cleanup();
    resetFreshnessStore();
    stubFetch({ detail: "Too many requests" }, 429);
    mount();
    await waitFor(() => expect(document.getElementById("shell-freshness")).toHaveTextContent("freshness check unavailable"));
  });

  it("is plain text: no live region and no link, so it is never announced on its own", async () => {
    stubFetch(HEALTHY);
    mount();
    const banner = screen.getByRole("banner");
    await waitFor(() => expect(banner).toHaveTextContent("Data as of"));
    expect(banner.querySelector("[aria-live], [role=status], output")).toBeNull();
    expect(document.getElementById("shell-freshness")?.querySelector("*")).toBeNull();
  });

  it("asks /api/freshness once for a page load, across StrictMode and every route change, and never /api/stats for it", async () => {
    const fn = stubFetch(HEALTHY);
    const user = userEvent.setup();
    mount("/method/", true);
    await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("Data as of"));
    const nav = within(screen.getByRole("navigation", { name: "Main" }));
    await user.click(nav.getByRole("link", { name: "Company" }));
    await user.click(nav.getByRole("link", { name: "Ask" }));
    await user.click(nav.getByRole("link", { name: "Workspace" }));
    await act(async () => {});
    expect(calls(fn, "/api/freshness")).toBe(1);
  });

  it("on the Freshness page the shell and the page share the one request, and the line is not repeated in the main area", async () => {
    const fn = stubFetch(HEALTHY);
    mount("/freshness/", true);
    await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("Data as of 2026-09-30"));
    await waitFor(() => expect(within(screen.getByRole("main")).getByRole("list", { name: "Freshness fields" })).toBeInTheDocument());
    expect(calls(fn, "/api/freshness")).toBe(1);
    expect(screen.getAllByText(/Data as of 2026-09-30 \u00b7 checked/)).toHaveLength(2); // banner + the page's own line, once each
  });

  it("keeps the header on the clock of its own load", async () => {
    const MIN = 60000;
    const t0 = Date.now();
    const clock = vi.spyOn(Date, "now").mockReturnValue(t0);
    try {
      stubFetch({ ...HEALTHY, checked_at: new Date(t0 - 5 * MIN).toISOString() });
      mount("/method/");
      await waitFor(() => expect(screen.getByRole("banner")).toHaveTextContent("checked 5 min ago"));
      clock.mockReturnValue(t0 + 60 * MIN);
      await act(async () => {});
      expect(screen.getByRole("banner")).toHaveTextContent("checked 5 min ago");
    } finally {
      clock.mockRestore();
    }
  });
});
