// R20 (docs/specs/m5b-frontend.md): the text shown for a non-OK API response. The server's JSON `detail` (its MSG_* copy) is
// shown unchanged; anything else (an HTML error page, malformed JSON, a validation list, a body already read) falls back to
// the status text, as the legacy page does, or names the status when there is none (HTTP/2 sends no reason phrase). Never
// the body itself, so an upstream HTML page is never rendered. Main-owned: worker A and worker C1 both render through it.

function jsonDetail(body: unknown): string | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return null;
  }
  const detail = (body as { detail?: unknown }).detail;
  return typeof detail === "string" && detail.trim() !== "" ? detail : null;
}

export async function detailOf(response: Response): Promise<string> {
  try {
    const detail = jsonDetail(await response.json());
    if (detail !== null) {
      return detail;
    }
  } catch {
    // not JSON, or the body was already read: the status text below
  }
  return response.statusText || `HTTP ${response.status}`;
}
