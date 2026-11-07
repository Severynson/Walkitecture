import { FIXTURE_NAMES } from "./fixtures";

/** Links to every committed fixture (no Babylon.js import, so it stays in the small main chunk). */
export default function FixtureLinks() {
  return (
    <ul>
      {FIXTURE_NAMES.map((n) => (
        <li key={n}>
          <a href={`/viewer?fixture=${encodeURIComponent(n)}`}>{n}</a>
        </li>
      ))}
    </ul>
  );
}
