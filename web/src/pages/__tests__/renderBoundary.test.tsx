import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RenderBoundary } from "../AskParts";

// The answer boundary is not remounted per delta (focus, A2), so a failure is lifted by a new resetKey instead.

function Thrower({ fail }: { fail: boolean }) {
  if (fail) throw new Error("boom");
  return <button type="button">ok</button>;
}

const mount = (fail: boolean, resetKey: number, onError = vi.fn()) => (
  <RenderBoundary fallback={<p>fallback</p>} onError={onError} resetKey={resetKey}>
    <Thrower fail={fail} />
  </RenderBoundary>
);

describe("RenderBoundary", () => {
  it("shows the fallback and reports once when the children throw", () => {
    const onError = vi.fn();
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(mount(true, 1, onError));
    expect(screen.getByText("fallback")).toBeInTheDocument();
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("stays failed while the resetKey is unchanged, and tries the children again when it changes", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const view = render(mount(true, 1));
    view.rerender(mount(false, 1));
    expect(screen.getByText("fallback")).toBeInTheDocument();
    view.rerender(mount(false, 2));
    expect(screen.getByRole("button", { name: "ok" })).toBeInTheDocument();
  });

  it("keeps the very same DOM node across a resetKey change when nothing failed", () => {
    const view = render(mount(false, 1));
    const before = screen.getByRole("button", { name: "ok" });
    view.rerender(mount(false, 2));
    expect(screen.getByRole("button", { name: "ok" })).toBe(before);
  });
});
