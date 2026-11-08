import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Logo } from "../brand/Logo";
import PlanDrawing from "./PlanDrawing";

const SAMPLE_URL = "/viewer?fixture=call-center-offices";

const STEPS = [
  {
    title: "Upload the drawing",
    body: "Drop in a .dwg or .dxf straight from CAD, up to 25 MB. Nothing to install.",
  },
  {
    title: "We read the plan",
    body: "Layers and blocks become walls, doors, windows, rooms and furniture, with AI to help tidy up messy drawings.",
  },
  {
    title: "Walk it",
    body: "Step inside at a 6 ft eye height. WASD to walk, Shift to run, T to look down on the whole plan.",
  },
  {
    title: "Change it",
    body: "Resize windows, raise the ceiling, or just ask in plain words. Every change can be undone.",
  },
];

/** `/`: public landing page. */
export default function LandingPage() {
  const { user } = useAuth();
  return (
    <div className="landing">
      <header className="site-header">
        <Logo />
        <nav className="site-nav" aria-label="Account">
          {user ? (
            <Link className="button primary" to="/plans">
              Your plans →
            </Link>
          ) : (
            <>
              <Link className="button ghost" to="/login">
                Log in
              </Link>
              <Link className="button primary" to="/register">
                Sign up
              </Link>
            </>
          )}
        </nav>
      </header>

      <main>
        <section className="hero">
          <div className="hero-copy">
            <p className="eyebrow">Sheet A-001 · DWG to walkable 3D</p>
            <h1 className="hero-title">
              Don't just see the floor plan. <span className="hero-walk">Walk it.</span>
            </h1>
            <p className="lede">
              Upload an AutoCAD drawing and Walkitecture reads its walls, doors, windows and rooms, furnishes the space,
              and lets you walk through it at eye level, right in your browser.
            </p>
            <div className="row hero-actions">
              <Link className="button primary lg" to={user ? "/plans" : "/register"}>
                Upload a DWG
              </Link>
              <a className="button lg" href={SAMPLE_URL}>
                Walk a sample office →
              </a>
            </div>
          </div>
          <figure className="sheet hero-sheet">
            <PlanDrawing />
            <figcaption className="sheet-caption">
              <span>Plan · Level 1</span>
              <span>Walker eye height 6′-0″</span>
            </figcaption>
          </figure>
        </section>

        <section className="steps" aria-labelledby="steps-title">
          <header className="section-head">
            <p className="eyebrow">Sequence</p>
            <h2 id="steps-title" className="section-title">
              From drawing to doorway
            </h2>
          </header>
          <ol className="step-list">
            {STEPS.map((step, i) => (
              <li key={step.title} className="sheet step">
                <span className="callout" aria-hidden>
                  <span>{i + 1}</span>
                  <span>A-{i + 2}</span>
                </span>
                <h3>{step.title}</h3>
                <p className="muted">{step.body}</p>
              </li>
            ))}
          </ol>
        </section>
      </main>

      <footer className="title-block" aria-label="Title block">
        <div className="tb-cell tb-project">
          <span className="tb-key">Project</span>
          <Logo to={null} />
        </div>
        <div className="tb-cell">
          <span className="tb-key">Drawing</span>
          <span>Floor plan → walkthrough</span>
        </div>
        <div className="tb-cell">
          <span className="tb-key">Scale</span>
          <span>1 : 1, you're in it</span>
        </div>
        <div className="tb-cell">
          <span className="tb-key">Issued</span>
          <span>Capital Hacks 2026</span>
        </div>
        <div className="tb-cell">
          <span className="tb-key">Sheet</span>
          <span>A-001</span>
        </div>
      </footer>
    </div>
  );
}
