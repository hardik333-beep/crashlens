// A copy-to-clipboard button that briefly confirms the copy. Used for keys and
// invite links.
import { useCallback, useRef, useState } from "react";

export function CopyButton({
  value,
  label = "Copy",
}: {
  value: string;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);

  const onCopy = useCallback(() => {
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      if (timer.current !== null) {
        window.clearTimeout(timer.current);
      }
      timer.current = window.setTimeout(() => setCopied(false), 1500);
    });
  }, [value]);

  return (
    <button type="button" className="btn btn-ghost" onClick={onCopy}>
      {copied ? "Copied" : label}
    </button>
  );
}
