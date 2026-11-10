// docs/specs/m5b-redesign.md RD20 and RD22, over lib/companyView.ts and worded by copy/deck.ts company. "At a glance": revenue,
// net income and R&D for the latest fiscal year with the prior year beneath, and the rules the page lists (decision 3).
// "Key numbers": the company's four latest fiscal years as columns (D-R2), metrics as row headers with a plain meaning, compact
// values in the reported currency (never a default "$") with the exact value in the stamp's name, a "Show exact values" switch
// for the table only, and the "not in our data" wording (D-R4, decision 1). Every figure is a FigureStamp (SR3).
import { Switch } from "@base-ui/react/switch";
import { useId, useState } from "react";
import { FigureStamp } from "@/components/Chips";
import { deck } from "@/copy/deck";
import { CITE_GROUP, type Metric } from "@/lib/company";
import { fiscalColumns, glance, numberRows, type Cell, type FiscalColumn, type NumberRow } from "@/lib/companyView";
import { compactMoney, exactMoney } from "@/lib/format";
import { focusSection } from "@/lib/sectionHash";
import { Block, ScrollRegion } from "./CompanySection";

const w = deck.company.numbers;
const NONE = deck.company.status.none;

function metricName(row: Pick<NumberRow, "metric" | "known">): string {
  return row.known ? w.metric[row.metric as keyof typeof w.metric].name : row.metric;
}

function Figure({ cell, metric, column, exact, gen }: { cell: Cell; metric: string; column: FiscalColumn; exact: boolean; gen: number }) {
  const compact = compactMoney(cell.value, cell.unit);
  const full = exactMoney(cell.value, cell.unit);
  const shown = exact ? full : compact;
  return <FigureStamp cite={cell.cite} place="company" gen={gen} nth={0} shown={shown} name={w.stampName({ shown, metric, year: column.long, exact: full })} />;
}

/** The text of a metric with no figure in these years: the latest year held (D-R4), or nothing held at all (decision 1). */
const missing = (row: Pick<NumberRow, "latestHeld">): string => (row.latestHeld === null ? w.absent : w.notHeld(row.latestHeld));

function NumbersTable({ rows, columns, exact }: { rows: NumberRow[]; columns: FiscalColumn[]; exact: boolean }) {
  return (
    <ScrollRegion label={w.tableRegion}>
      <table aria-labelledby="key-numbers" className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-border">
            <th scope="col" className="px-3 py-2 text-left font-semibold">
              {w.metricColumn}
            </th>
            {columns.map((col) => (
              <th key={col.periodEnd} scope="col" className="px-3 py-2 text-right font-semibold whitespace-nowrap">
                {col.long}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.metric} className="border-b border-border">
              <th scope="row" className="px-3 py-2 text-left align-top font-semibold">
                {metricName(row)}
                {row.known && <span className="block text-xs font-normal text-muted-foreground">{w.metric[row.metric as keyof typeof w.metric].meaning}</span>}
              </th>
              {row.cells.every((cell) => cell === null) ? (
                <td colSpan={columns.length} className="px-3 py-2 align-top text-muted-foreground">
                  {missing(row)}
                </td>
              ) : (
                row.cells.map((cell, i) => (
                  <td key={columns[i].periodEnd} className="px-3 py-2 text-right align-top whitespace-nowrap">
                    {cell === null ? null : <Figure cell={cell} metric={metricName(row)} column={columns[i]} exact={exact} gen={CITE_GROUP.metrics} />}
                  </td>
                ))
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </ScrollRegion>
  );
}

export function KeyNumbers({ metrics }: { metrics: Metric[] }) {
  const [exact, setExact] = useState(false);
  const labelId = useId();
  const columns = fiscalColumns(metrics);
  return (
    <Block id="key-numbers" title={w.heading}>
      {columns.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <>
          <p className="mt-0 mb-3 text-sm text-muted-foreground">{w.fiscalNote}</p>
          <div className="mb-3 flex min-h-[24px] items-center gap-2 text-sm">
            <Switch.Root
              checked={exact}
              onCheckedChange={setExact}
              aria-labelledby={labelId}
              className="relative inline-flex h-[24px] w-[44px] shrink-0 cursor-pointer items-center rounded-full border-2 border-border-strong bg-muted p-[2px] data-[checked]:border-primary data-[checked]:bg-primary"
            >
              <Switch.Thumb className="block size-[16px] rounded-full border border-border-strong bg-card data-[checked]:translate-x-[20px]" />
            </Switch.Root>
            <span id={labelId}>{w.exact}</span>
          </div>
          <NumbersTable rows={numberRows(metrics, columns)} columns={columns} exact={exact} />
        </>
      )}
    </Block>
  );
}

function GlanceFigure({ cell, metric, column }: { cell: Cell | null; metric: string; column: FiscalColumn | undefined }) {
  if (cell === null || column === undefined) return null;
  return <Figure cell={cell} metric={metric} column={column} exact={false} gen={CITE_GROUP.glance} />;
}

export function AtAGlance({ metrics, rulesListed }: { metrics: Metric[]; rulesListed: number }) {
  const columns = fiscalColumns(metrics);
  const figures = glance(metrics, columns);
  const g = deck.company.glance;
  return (
    <Block id="at-a-glance" title={g.heading}>
      <ul className="m-0 grid list-none grid-cols-1 gap-3 p-0 sm:grid-cols-3">
        {figures.map((f) => {
          const name = w.metric[f.metric].name;
          return (
            <li key={f.metric} className="rounded-md border border-border bg-card p-3">
              <p className="m-0 text-sm font-semibold">{name}</p>
              {f.latest === null ? (
                (f.absent || f.latestHeld !== null) && <p className="m-0 mt-1 text-sm text-muted-foreground">{missing(f)}</p>
              ) : (
                <p className="m-0 mt-1 text-lg">
                  <GlanceFigure cell={f.latest} metric={name} column={columns[0]} />
                </p>
              )}
              {f.prior !== null && columns[1] !== undefined && (
                <p className="m-0 mt-1 text-sm text-muted-foreground">
                  {`${columns[1].short}: `}
                  <GlanceFigure cell={f.prior} metric={name} column={columns[1]} />
                </p>
              )}
            </li>
          );
        })}
      </ul>
      {rulesListed > 0 && (
        <p className="mt-3 mb-0 text-sm">
          <a
            href="#government-rules"
            onClick={() => focusSection("government-rules")}
            className="inline-flex min-h-[24px] items-center text-primary underline underline-offset-4"
          >
            {g.rulesCount(rulesListed)}
          </a>
        </p>
      )}
    </Block>
  );
}
