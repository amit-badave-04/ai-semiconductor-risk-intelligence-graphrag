import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useEvidenceDrawer } from "@/components/useEvidenceDrawer";
import { useAsk } from "../useAsk";
import { useWorkspaceUploads } from "../useWorkspaceUploads";

// Outside a provider each hook is inert (component tests stay simple): its functions exist, do nothing, and never throw.
describe("the hooks outside their provider", () => {
  it("useAsk is idle and its actions do nothing", () => {
    const { result } = renderHook(() => useAsk());
    expect(result.current.status).toBe("idle");
    expect(() => {
      result.current.ask({ question: "q" });
      result.current.abort();
      result.current.reset();
    }).not.toThrow();
    expect(result.current.status).toBe("idle");
  });

  it("useEvidenceDrawer is closed and its actions do nothing", () => {
    const { result } = renderHook(() => useEvidenceDrawer());
    expect(result.current.openId).toBeNull();
    result.current.open("chunk:1", null, "answer");
    result.current.close();
    result.current.dropDocEvidence();
    result.current.clearAll();
    expect(result.current.openId).toBeNull();
  });

  it("useWorkspaceUploads has nothing and its handlers resolve empty", async () => {
    const { result } = renderHook(() => useWorkspaceUploads());
    expect(result.current.documents).toEqual([]);
    expect(result.current.rows).toEqual([]);
    expect(result.current.announcement).toBe("");
    expect(await result.current.create()).toBe("");
    expect(await result.current.restore("a", "b")).toBe("");
    await expect(result.current.remove()).resolves.toBeUndefined();
    expect(() => result.current.upload([], "")).not.toThrow();
  });
});
