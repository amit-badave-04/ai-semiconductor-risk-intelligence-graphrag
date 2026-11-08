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

describe("detailOf reads at most 64 KiB of a non-OK body (security LOW)", () => {
  const CAP = 64 * 1024;
  const jsonOfSize = (bytes: number): string => {
    const head = '{"detail":"';
    const tail = '"}';
    return head + "x".repeat(bytes - head.length - tail.length) + tail;
  };

  it("shows the detail of a body of exactly 64 KiB", async () => {
    const body = jsonOfSize(CAP);
    expect(body.length).toBe(CAP);
    expect(await detailOf(respond(body, { status: 400, statusText: "Bad Request" }))).toBe(body.slice(11, -2));
  });

  it("falls back to the status text for a valid JSON body one byte over the cap", async () => {
    const body = jsonOfSize(CAP + 1);
    expect(await detailOf(respond(body, { status: 400, statusText: "Bad Request" }))).toBe("Bad Request");
  });

  it("falls back to the status text for a far larger JSON body", async () => {
    const body = jsonOfSize(5 * CAP);
    expect(await detailOf(respond(body, { status: 500, statusText: "Server Error" }))).toBe("Server Error");
  });

  it("counts bytes, not characters: a multi-byte body over the cap in bytes falls back", async () => {
    const body = `{"detail":"${"\u00e9".repeat(CAP / 2)}"}`; // 2 bytes per character: about 64 KiB + 14 bytes
    expect(body.length).toBeLessThan(CAP);
    expect(await detailOf(respond(body, { status: 400, statusText: "Bad Request" }))).toBe("Bad Request");
  });

  it("a body that keeps enqueuing resolves promptly to the status text and releases the stream", async () => {
    let pulls = 0;
    let cancelled = false;
    const chunk = new TextEncoder().encode("x".repeat(8192));
    const stream = new ReadableStream<Uint8Array>(
      {
        pull(controller) {
          pulls += 1;
          if (pulls > 2000) {
            controller.close(); // a stand-in for "never": a mutant without the cap would read all 16 MB
          } else {
            controller.enqueue(chunk);
          }
        },
        cancel() {
          cancelled = true;
        },
      },
      { highWaterMark: 0 },
    );
    const response = new Response(stream, { status: 502, statusText: "Bad Gateway", headers: { "content-type": "application/json" } });
    expect(await detailOf(response)).toBe("Bad Gateway");
    expect(pulls).toBeLessThan(20);
    expect(cancelled).toBe(true);
  });

  it("a body-less response falls back to the status text", async () => {
    expect(await detailOf(new Response(null, { status: 503, statusText: "Unavailable" }))).toBe("Unavailable");
  });
});

describe("detailOf: a body that cannot be cancelled", () => {
  it("still answers with the status text when cancelling the oversized body fails", async () => {
    const chunk = new Uint8Array(40 * 1024);
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(chunk);
      },
      cancel() {
        throw new Error("cannot cancel");
      },
    });
    const response = new Response(stream, { status: 502, statusText: "Bad Gateway" });
    expect(await detailOf(response)).toBe("Bad Gateway");
  });
});
