import { useTurnstile } from "@/state/useTurnstile";

/**
 * The element the single Turnstile widget is drawn in (R33(3)). Rendered once by the app shell, below the router's layout, so
 * it persists across routes. Empty (and hidden) until a widget exists: with no site key nothing is ever drawn here.
 */
export function TurnstileWidget() {
  const { registerHost } = useTurnstile();
  return <div ref={registerHost} data-turnstile-host="" className="mx-auto max-w-[1100px] px-5 pb-6 empty:hidden" />;
}
