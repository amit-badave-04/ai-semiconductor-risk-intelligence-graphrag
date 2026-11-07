import { describe, expect, it } from "vitest";
import { detailOf } from "../refusal";

// R20 (docs/specs/m5b-frontend.md): a non-OK response is rendered from its JSON `detail`; the server's MSG_* copy is shown
// as sent, and nothing new is added. The legacy page (index.html) falls back to the status text, and so does this.
function respond(body: string, init: { status: number; statusText?: string; contentType?: string }): Response {
  return new Response(body, {
    status: init.status,
    statusText: init.statusText ?? "",
    headers: { "content-type": init.contentType ?? "application/json" },
  });
}

describe("detailOf (R20: a refusal is shown from its JSON detail)", () => {
  it.each([
    [404, "workspace not found"],
    [503, "Uploaded documents are not available right now."],
    [429, "Too many questions from your address - please wait a few minutes."],
  ])("a %i with a JSON detail shows that detail unchanged", async (status, detail) => {
    expect(await detailOf(respond(JSON.stringify({ detail }), { status, statusText: "Refused" }))).toBe(detail);
  });

  it("adds nothing for a Retry-After header (no countdown, no new behaviour)", async () => {
    const response = new Response(JSON.stringify({ detail: "workspace not found" }), {
      status: 503,
      headers: { "content-type": "application/json", "retry-after": "30" },
    });
    expect(await detailOf(response)).toBe("workspace not found");
  });

  it.each([
    ["an HTML error page", "<html><body><h1>502 Bad Gateway</h1></body></html>", "text/html"],
    ["malformed JSON", "{detail:", "application/json"],
    ["JSON without a detail", JSON.stringify({ error: "x" }), "application/json"],
    ["a validation error list", JSON.stringify({ detail: [{ loc: ["body"], msg: "field required" }] }), "application/json"],
    ["a non-string detail", JSON.stringify({ detail: 42 }), "application/json"],
    ["a blank detail", JSON.stringify({ detail: "   " }), "application/json"],
    ["a bare JSON string", JSON.stringify("<b>not an object</b>"), "application/json"],
  ])("%s falls back to the status text, never to the body", async (_name, body, contentType) => {
    expect(await detailOf(respond(body, { status: 502, statusText: "Bad Gateway", contentType }))).toBe("Bad Gateway");
  });

  it("names the status when there is no status text either (HTTP/2 sends none)", async () => {
    expect(await detailOf(respond("<html></html>", { status: 502, contentType: "text/html" }))).toBe("HTTP 502");
  });

  it("never throws, whatever the body", async () => {
    const used = respond(JSON.stringify({ detail: "workspace not found" }), { status: 404, statusText: "Not Found" });
    await used.text();
    expect(await detailOf(used)).toBe("Not Found");
  });
});
