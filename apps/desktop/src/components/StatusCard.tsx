type StatusCardProps = {
  title: string;
  description: string;
  status: string;
};

export function StatusCard({ title, description, status }: StatusCardProps) {
  return (
    <div className="rounded-3xl border border-white/60 bg-white/70 p-6 shadow-panel backdrop-blur">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-lg font-semibold text-ink">{title}</h3>
        <span className="rounded-full bg-accent/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-accent">
          {status}
        </span>
      </div>
      <p className="text-sm leading-6 text-slate-600">{description}</p>
    </div>
  );
}

