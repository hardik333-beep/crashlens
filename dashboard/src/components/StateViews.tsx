// Shared loading / error / empty presentational blocks so every page renders the
// three states the same way.
import type { ReactNode } from "react";

export function LoadingView({ label }: { label?: string }) {
  return <p className="muted">{label ?? "Loading..."}</p>;
}

export function ErrorView({ message }: { message: string }) {
  return (
    <p role="alert" className="error-text">
      {message}
    </p>
  );
}

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <p className="empty-title">{title}</p>
      {children}
    </div>
  );
}
