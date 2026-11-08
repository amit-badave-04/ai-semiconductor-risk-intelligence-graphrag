// S3: the only way a string from the server or a model becomes an anchor's href. Structured URL fields (the evidence metadata)
// go through here; answer, evidence and what-changed TEXT is never linkified.

// Whitespace and every control character, anywhere: `new URL` silently strips leading whitespace and tabs and newlines inside a
// scheme (`java\nscript:`), so the raw string is checked before it is parsed.
function hasForbiddenCharacter(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code <= 0x20 || (code >= 0x7f && code <= 0x9f)) return true;
  }
  return false;
}

// Lowercase `https://` and then a host character: `https:///x` (a URL parser would silently read x as the host) is refused.
const HTTPS_AUTHORITY = /^https:\/\/[^/\\]/;

/**
 * The normalized href of `value` when it is a plain `https:` URL, else null (the caller shows the value as text).
 * The raw string must start with the lowercase `https://`, hold no whitespace or control character, parse with `new URL()`,
 * have protocol exactly `https:`, a host, and no embedded credentials. Anything else (`javascript:`, `data:`, `vbscript:`,
 * `blob:`, `mailto:`, `http:`, `//host`, a path, a non-string) is null.
 */
export function safeUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  if (!HTTPS_AUTHORITY.test(value) || hasForbiddenCharacter(value)) return null;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.hostname === "" || url.username !== "" || url.password !== "") return null;
  return url.href;
}

/** The `rel` every anchor built from a safeUrl carries. */
export const SAFE_REL = "noopener noreferrer";
