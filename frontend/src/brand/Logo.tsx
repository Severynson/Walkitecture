import { Link } from "react-router-dom";

/** The mark: a two-room plan drawn in ink, with a walker entering through the front door. */
export function LogoMark({ size = 28 }: { size?: number }) {
  return (
    <svg className="logo-mark" width={size} height={size} viewBox="0 0 32 32" aria-hidden focusable="false">
      <path d="M12 30H2V2h28v28H20M16 2v11" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="square" />
      <path d="M16 31c0-9 7-10 7-16" fill="none" stroke="var(--walk)" strokeWidth="1.8" strokeDasharray="2 2.6" />
      <circle cx="23" cy="11" r="3" fill="var(--walk)" />
    </svg>
  );
}

/** Mark + "WALKitecture" wordmark; links home unless `to` is null. */
export function Logo({ to = "/" }: { to?: string | null }) {
  const body = (
    <>
      <LogoMark />
      <span className="wordmark">
        <span className="wordmark-walk">Walk</span>itecture
      </span>
    </>
  );
  return to === null ? (
    <span className="logo">{body}</span>
  ) : (
    <Link to={to} className="logo" aria-label="Walkitecture home">
      {body}
    </Link>
  );
}
