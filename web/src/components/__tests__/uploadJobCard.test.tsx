import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JobView } from "@/lib/jobView";
import { UploadJobCard } from "../UploadJobCard";

const pre = { form: "10-K", sections: ["Item 1A <b>Risk</b>", "Item 7"], sectionsMessage: "", chunks: 40, pendingChunks: 40 };
const view = (extra: Partial<JobView> = {}): JobView => ({ keywordReady: true, preflight: pre, embedding: null, outcome: null, ...extra });
const noop = (): void => undefined;

describe("UploadJobCard (M2 R19, the council wording)", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(0);
  });
  afterEach(() => vi.useRealTimers());

  it("renders nothing before keyword_ready", () => {
    const { container } = render(<UploadJobCard view={view({ keywordReady: false, preflight: null })} onCancel={noop} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the preflight, a gloss for passage, keyword search ready, and a waiting job before embedding starts", () => {
    render(<UploadJobCard view={view()} onCancel={noop} />);
    expect(screen.getByText("What we found in this file")).toBeInTheDocument();
    expect(screen.getByText("A passage is a short piece of your file.")).toBeInTheDocument();
    expect(screen.getByText("Form: 10-K")).toBeInTheDocument();
    expect(screen.getByText("40 passages to search")).toBeInTheDocument();
    expect(screen.getByText("Search by meaning: time estimate shown once it starts. This can take up to an hour.")).toBeInTheDocument();
    expect(screen.getByText("Keyword search is ready. You can ask questions now.")).toBeInTheDocument();
    expect(screen.getByText("Search by meaning is waiting to start. Keyword search works now.")).toBeInTheDocument();
  });

  it("renders section labels as escaped text, never as HTML", () => {
    const { container } = render(<UploadJobCard view={view()} onCancel={noop} />);
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText(/Item 1A <b>Risk<\/b>, Item 7/)).toBeInTheDocument();
  });

  it("says no standard sections were found, and what still works when the form is not recognised", () => {
    const sectionsMessage = "The form type was not recognised; the document is embedded in document order.";
    render(<UploadJobCard view={view({ preflight: { ...pre, form: null, sections: [], sectionsMessage } })} onCancel={noop} />);
    expect(screen.getByText("Form: not recognised. You can still ask about this file.")).toBeInTheDocument();
    expect(screen.getByText("No standard sections found.")).toBeInTheDocument();
    expect(screen.queryByText(/embedded in document order/)).toBeNull();
  });

  it("says passage in the singular in the preflight", () => {
    render(<UploadJobCard view={view({ preflight: { ...pre, chunks: 1 } })} onCancel={noop} />);
    expect(screen.getByText("1 passage to search")).toBeInTheDocument();
  });

  it("shows progress, the estimate in minutes, and a stop button that calls onCancel and then says Stopping", async () => {
    const onCancel = vi.fn();
    render(<UploadJobCard view={view({ embedding: { done: 6, total: 40, etaS: 4.2 } })} onCancel={onCancel} />);
    expect(screen.getByText("Preparing search by meaning: 6 of 40 passages")).toBeInTheDocument();
    expect(screen.getByText("Search by meaning: less than a minute left (estimate)")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Preparing search by meaning" })).toHaveAttribute("aria-valuenow", "6");
    await userEvent.click(screen.getByRole("button", { name: "Stop preparing search by meaning" }));
    expect(onCancel).toHaveBeenCalledOnce();
    const pending = screen.getByRole("button", { name: "Stopping…" });
    expect(pending).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(pending);
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("a refused or failed stop gives the button back so the user can try again", async () => {
    const onCancel = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    render(<UploadJobCard view={view({ embedding: { done: 6, total: 40, etaS: 4.2 } })} onCancel={onCancel} />);
    await userEvent.click(screen.getByRole("button", { name: "Stop preparing search by meaning" }));
    const again = await screen.findByRole("button", { name: "Stop preparing search by meaning" });
    expect(again).not.toHaveAttribute("aria-disabled");
    await userEvent.click(again);
    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(await screen.findByRole("button", { name: "Stopping…" })).toHaveAttribute("aria-disabled", "true");
  });

  it("an estimate of 0 seconds reads as less than a minute, not as unknown", () => {
    render(<UploadJobCard view={view({ embedding: { done: 6, total: 40, etaS: 0 } })} onCancel={noop} />);
    expect(screen.getByText("Search by meaning: less than a minute left (estimate)")).toBeInTheDocument();
  });

  it("describes the stop button by its helper sentence, and the file name", () => {
    render(
      <>
        <span id="n1">a.txt</span>
        <UploadJobCard view={view()} nameId="n1" onCancel={noop} />
      </>,
    );
    const button = screen.getByRole("button", { name: "Stop preparing search by meaning" });
    expect(button).toHaveAccessibleDescription(/Keyword search keeps working\. Passages already prepared stay searchable by meaning\./);
    expect(button).toHaveAccessibleDescription(/a\.txt/);
  });

  it("the live region holds keyword-ready, the start, and the end line, and not every progress count", () => {
    const { container, rerender } = render(<UploadJobCard view={view({ embedding: { done: 1, total: 8, etaS: null } })} onCancel={noop} />);
    const live = (): Element | null => container.querySelector('[aria-live="polite"]');
    expect(live()).toHaveTextContent("Keyword search is ready. You can ask questions now.");
    expect(live()).toHaveTextContent("Preparing search by meaning started.");
    expect(live()).not.toHaveTextContent("1 of 8");
    rerender(<UploadJobCard view={view({ embedding: { done: 2, total: 8, etaS: null } })} onCancel={noop} />);
    expect(live()).not.toHaveTextContent("2 of 8");
    const outcome = { embeddedChunks: 2, keywordOnlyChunks: 0, stopped: null, search: "keyword_semantic" as const };
    rerender(<UploadJobCard view={view({ outcome })} onCancel={noop} />);
    expect(live()).toHaveTextContent("All 2 passages can be searched by keyword and by meaning.");
    expect(live()).not.toHaveTextContent("started");
  });

  it("speaks a progress line only at a 25% step and at most once a minute", () => {
    const { container, rerender } = render(<UploadJobCard view={view({ embedding: { done: 0, total: 8, etaS: null } })} onCancel={noop} />);
    const live = (): Element | null => container.querySelector('[aria-live="polite"]');
    vi.setSystemTime(30_000);
    rerender(<UploadJobCard view={view({ embedding: { done: 3, total: 8, etaS: null } })} onCancel={noop} />);
    expect(live()).not.toHaveTextContent("3 of 8"); // a step, but under a minute since the start line
    vi.setSystemTime(61_000);
    rerender(<UploadJobCard view={view({ embedding: { done: 4, total: 8, etaS: null } })} onCancel={noop} />);
    expect(live()).toHaveTextContent("Preparing search by meaning: 4 of 8 passages");
  });

  it("hides progress and the stop button once the row ended without an outcome", () => {
    render(<UploadJobCard view={view({ embedding: { done: 1, total: 2, etaS: null } })} terminal onCancel={noop} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText(/1 of 2/)).toBeNull();
    expect(screen.queryByText(/waiting to start/)).toBeNull();
  });

  it("ends with the council end lines and no stop button", () => {
    const o = (e: number, k: number, stopped: string | null = null) => ({ embeddedChunks: e, keywordOnlyChunks: k, stopped, search: null });
    const { rerender } = render(<UploadJobCard view={view({ outcome: o(1, 0) })} onCancel={noop} />);
    expect(screen.getByText("The 1 passage can be searched by keyword and by meaning.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText(/time estimate|left \(estimate\)/)).toBeNull();
    rerender(<UploadJobCard view={view({ outcome: o(0, 0) })} onCancel={noop} />);
    expect(screen.getByText("No text was found to search in this file.")).toBeInTheDocument();
    rerender(<UploadJobCard view={view({ outcome: o(30, 10, "timeout") })} onCancel={noop} />);
    expect(screen.getByText("Search by meaning stopped early. 30 passages can be searched by keyword and by meaning. 10 can be searched by keyword only.")).toBeInTheDocument();
    rerender(<UploadJobCard view={view({ outcome: o(3, 9, "cancelled") })} onCancel={noop} />);
    expect(screen.getByText("Stopped. 3 passages can be searched by keyword and by meaning. 9 can be searched by keyword only.")).toBeInTheDocument();
  });
});
