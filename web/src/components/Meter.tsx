/** A limit and how much of it is used: green < 60%, amber < 90%, red beyond. */
export function Meter({
  name,
  used,
  text,
  limit,
  hint,
}: {
  name: string;
  used: number | null;
  text: string;
  limit: string;
  hint?: string;
}) {
  const u = used == null || Number.isNaN(used) ? 0 : Math.max(0, Math.min(1, used));
  const tone = u >= 0.9 ? "bad" : u >= 0.6 ? "warn" : "good";
  return (
    <div className="meter" title={hint}>
      <div className="meter-head">
        <span>{name}</span>
        <b>{text}</b>
      </div>
      <div className="bar" role="meter" aria-label={name} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(u * 100)}>
        <i className={tone} style={{ width: `${u * 100}%` }} />
      </div>
      <small className="mute">limit {limit}</small>
    </div>
  );
}
