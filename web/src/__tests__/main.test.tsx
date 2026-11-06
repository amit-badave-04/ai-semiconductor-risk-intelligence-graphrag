import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function addRoot() {
  const root = document.createElement("div");
  root.id = "root";
  document.body.replaceChildren(root);
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  window.history.pushState({}, "", "/");
});

describe("main.tsx mounts the app into #root", () => {
  it("renders the page of the current path, under the /v2 basename", async () => {
    window.history.pushState({}, "", "/v2/method/");
    addRoot();
    await import("../main");
    await waitFor(() => expect(screen.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeInTheDocument());
    expect(document.getElementById("root")).toContainElement(screen.getByRole("main"));
  });

  it("renders the same pages at the root once the server flips it", async () => {
    window.history.pushState({}, "", "/freshness/");
    addRoot();
    await import("../main");
    await waitFor(() => expect(screen.getByRole("heading", { level: 1, name: "Freshness" })).toBeInTheDocument());
  });

  it("fails loudly when index.html has no #root", async () => {
    document.body.replaceChildren();
    await expect(import("../main")).rejects.toThrow("#root");
  });
});
