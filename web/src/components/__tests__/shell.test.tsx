import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "@/App";
import { EvidenceDrawerContext, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import { resetConfigCache } from "@/lib/config";
import { resetTurnstileScriptCache } from "@/lib/turnstile";
import { AppRoutes } from "@/routes";
import { installFakeTurnstile, uninstallFakeTurnstile, type FakeTurnstile } from "@/test/fakeTurnstile";

// I3 main-first item 6: the shell mounts the providers in the contract order, one Turnstile host and one Evidence landmark above
// the router's pages, and a route change closes the drawer without taking focus from the new page's h1 (A7 wins over A5).

let fake: FakeTurnstile;

function stubConfig(key: string) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ turnstile_site_key: key }), { status: 200 })));
}

beforeEach(() => {
  resetConfigCache();
  resetTurnstileScriptCache();
  fake = installFakeTurnstile();
  window.history.pushState({}, "", "/v2/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  uninstallFakeTurnstile();
  document.head.querySelectorAll("script").forEach((s) => s.remove());
  window.history.pushState({}, "", "/");
});

describe("Layout and the drawer on a route change", () => {
  function spyDrawer() {
    const close = vi.fn<EvidenceDrawerApi["close"]>();
    const api: EvidenceDrawerApi = { openId: null, open: vi.fn(), close, dropDocEvidence: vi.fn(), clearAll: vi.fn() };
    return { ...api, close };
  }

  function mountWith(drawer: EvidenceDrawerApi) {
    return render(
      <EvidenceDrawerContext.Provider value={drawer}>
        <MemoryRouter initialEntries={["/"]}>
          <AppRoutes />
        </MemoryRouter>
      </EvidenceDrawerContext.Provider>,
    );
  }

  it("does not touch the drawer on the first render", () => {
    const drawer = spyDrawer();
    mountWith(drawer);
    expect(drawer.close).not.toHaveBeenCalled();
  });

  it("closes the drawer without restoring focus when a nav link is followed, and the h1 still takes focus", async () => {
    const drawer = spyDrawer();
    const user = userEvent.setup();
    mountWith(drawer);
    await user.click(screen.getByRole("link", { name: "How it works" }));
    expect(drawer.close).toHaveBeenCalledTimes(1);
    expect(drawer.close).toHaveBeenCalledWith({ restoreFocus: false });
    expect(screen.getByRole("heading", { level: 1, name: "Method & Evidence" })).toHaveFocus();
  });

  it("does not close the drawer when the same path is rendered again", async () => {
    const drawer = spyDrawer();
    const view = mountWith(drawer);
    view.rerender(
      <EvidenceDrawerContext.Provider value={{ ...drawer }}>
        <MemoryRouter initialEntries={["/"]}>
          <AppRoutes />
        </MemoryRouter>
      </EvidenceDrawerContext.Provider>,
    );
    expect(drawer.close).not.toHaveBeenCalled();
  });
});

describe("App: the providers and the shell widgets", () => {
  it("mounts one Turnstile host and one closed Evidence landmark, and asks /api/config once", async () => {
    stubConfig("");
    render(<App />);
    await act(async () => {});
    expect(document.querySelectorAll("[data-turnstile-host]")).toHaveLength(1);
    expect(document.querySelectorAll("aside[aria-label='Evidence']")).toHaveLength(1);
    expect(screen.queryByRole("complementary", { name: "Evidence" })).toBeNull();
    // The Ask page also GETs /api/examples and /api/stats, and the header GETs /api/freshness; only /api/config is the shell's config request.
    const configCalls = vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes("/api/config"));
    expect(configCalls).toHaveLength(1);
    expect(fake.renders).toBe(0);
  });

  it("the Turnstile host is outside the main landmark, so the page area stays as it was", async () => {
    stubConfig("");
    render(<App />);
    await act(async () => {});
    expect(screen.getByRole("main").querySelector("[data-turnstile-host]")).toBeNull();
  });

  it("with a site key, one widget is live and a route change does not render, remove or reset it", async () => {
    stubConfig("dummy-site-key");
    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(fake.live.size).toBe(1));
    const before = { renders: fake.renders, removes: fake.removes, resets: fake.resets };
    await user.click(screen.getByRole("link", { name: "How it works" }));
    await act(async () => {});
    expect({ renders: fake.renders, removes: fake.removes, resets: fake.resets }).toEqual(before);
    expect(fake.live.size).toBe(1);
    expect(fake.frames()).toBe(1);
  });

  it("sends no POST by loading a page (S12)", async () => {
    stubConfig("");
    render(<App />);
    await act(async () => {});
    const methods = vi.mocked(fetch).mock.calls.map((call) => (call[1] as RequestInit | undefined)?.method ?? "GET");
    expect(methods.every((m) => m === "GET")).toBe(true);
  });
});
