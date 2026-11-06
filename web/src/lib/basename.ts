// The router basename, chosen at runtime from the path (docs/specs/m5b-frontend.md R12), NOT from import.meta.env.BASE_URL
// (always /v2/ in this build) and NOT from a <base> element (the server's CSP has base-uri 'none').
//
// Under /v2/ the pages live at /v2/, /v2/company/NVDA/ ...; once the server flips the root (UI_V2) the same build answers at
// /, /company/NVDA/ ... and the basename is empty. /v2/ stays an alias after the flip, so the prefix test decides, not the flag.
const V2_PREFIX = "/v2";

export function basenameFor(pathname: string): string {
  return pathname === V2_PREFIX || pathname.startsWith(`${V2_PREFIX}/`) ? V2_PREFIX : "";
}
