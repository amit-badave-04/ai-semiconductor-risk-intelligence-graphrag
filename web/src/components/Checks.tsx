// R22: what the checks found, as visible warnings, and the hint that says what the checks do and do not prove. Text nodes only.
export interface ChecksNotesProps {
  warnings: readonly string[];
  hint: string;
}

export function ChecksNotes({ warnings, hint }: ChecksNotesProps) {
  return (
    <>
      {warnings.length > 0 && (
        <ul className="mt-1.5 list-disc pl-5 text-xs text-amber-800 dark:text-amber-400">
          {warnings.map((warning, index) => (
            <li key={index} className="my-0.5">
              {warning}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-1.5 text-xs">{hint}</div>
    </>
  );
}
