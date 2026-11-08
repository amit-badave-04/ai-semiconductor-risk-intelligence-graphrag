// R20 (docs/specs/m5b-frontend.md): the text shown for a non-OK API response. The server's JSON `detail` (its MSG_* copy) is
// shown unchanged; anything else (an HTML error page, malformed JSON, a validation list, a body already read, a body over
// 64 KiB) falls back to the status text, as the legacy page does, or names the status when there is none (HTTP/2 sends no
// reason phrase). Never the body itself, so an upstream HTML page is never rendered. The body is read through a stream
// reader and abandoned at the cap, so a huge or never-ending body is never buffered. Main-owned: worker A and worker C1
// both render through it.

/** The most bytes of a non-OK body that are read. A real refusal is one short sentence in a JSON object. */
export const MAX_DETAIL_BYTES = 64 * 1024;

function jsonDetail(body: unknown): string | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return null;
  }
  const detail = (body as { detail?: unknown }).detail;
  return typeof detail === "string" && detail.trim() !== "" ? detail : null;
}

/** The body as text, or null when it has none or is longer than MAX_DETAIL_BYTES (the reader is released at once). */
async function boundedText(response: Response): Promise<string | null> {
  if (response.body === null) {
    return null;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let total = 0;
  let text = "";
  try {
    for (;;) {
      const step = await reader.read();
      if (step.done) {
        return text + decoder.decode();
      }
      total += step.value.byteLength;
      if (total > MAX_DETAIL_BYTES) {
        return null;
      }
      text += decoder.decode(step.value, { stream: true });
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

export async function detailOf(response: Response): Promise<string> {
  try {
    const text = await boundedText(response);
    const detail = text === null ? null : jsonDetail(JSON.parse(text));
    if (detail !== null) {
      return detail;
    }
  } catch {
    // not JSON, or the body was already read: the status text below
  }
  return response.statusText || `HTTP ${response.status}`;
}
