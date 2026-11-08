import { useEffect, useState } from "react";

/** The walker's loop: hall → living → kitchen → bedroom → hall, passing through each doorway. */
const LOOP = "M230 260 Q200 260 200 230 L200 190 Q200 110 260 110 L340 110 Q400 110 400 165 L400 225 Q400 260 365 260 Z";

// 1 drawing unit = 3 cm: the outline is 400 × 280 units = 12.00 × 8.40 m.
const EXTERIOR = "M40 40H80 M200 40H300 M400 40H440V200 M440 280V320H210 M170 320H40V170 M40 90V40";
const INTERIOR = "M250 40V90 M250 130V240 M250 280V320 M40 210H180 M220 210H250 M150 210V240 M150 275V320 M250 150H380 M420 150H440";
const WINDOWS =
  "M80 37H200V43H80Z M80 40H200 M300 37H400V43H300Z M300 40H400 M437 200V280H443V200Z M440 200V280 M37 90V170H43V90Z M40 90V170";
const DOORS = [
  "M170 320V280 M210 320A40 40 0 0 0 170 280", // entry
  "M250 90H290 M250 130A40 40 0 0 0 290 90", // living → kitchen
  "M220 210V170 M180 210A40 40 0 0 1 220 170", // hall → living
  "M250 280H290 M250 240A40 40 0 0 1 290 280", // hall → bedroom
  "M150 240H115 M150 275A35 35 0 0 1 115 240", // hall → bath
  "M420 150V190 M380 150A40 40 0 0 0 420 190", // kitchen → bedroom
].join(" ");
const FURNITURE = [
  "M52 70h34v110h-34z M62 74v102", // sofa
  "M109 105h36v50h-36z", // coffee table
  "M262 46h172v22h-172z M326 50h30v14h-30z", // counter + sink
  "M290 170h76v70h-76z M296 176h30v14h-30z M332 176h30v14h-30z M290 200h76", // bed
  "M268 172h16v16h-16z", // nightstand
  "M46 218h38v70h-38z M52 224h26v58h-26z", // tub
  "M110 308h20v8h-20z", // toilet tank
].join(" ");

function usePrefersReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

/** Hero art: a blueprint apartment that draws itself in, then a walker (with its view cone) tours the rooms. */
export default function PlanDrawing() {
  const reduced = usePrefersReducedMotion();
  return (
    <svg className="plan-drawing" viewBox="0 0 500 380" role="img" aria-labelledby="plan-drawing-title">
      <title id="plan-drawing-title">
        Blueprint of a 12 by 8.4 metre apartment, with a walker touring the hall, living room, kitchen and bedroom
      </title>

      {/* Dimensions */}
      <g className="pd-dim pd-fade" style={{ animationDelay: "1.1s" }}>
        <path d="M40 18H440 M40 10V32 M440 10V32 M35 23L45 13 M435 23L445 13" />
        <path d="M18 40V320 M10 40H32 M10 320H32 M13 45L23 35 M13 325L23 315" />
        <text x="240" y="22" className="pd-dim-text">12.00 m</text>
        <text x="22" y="180" className="pd-dim-text" transform="rotate(-90 22 180)">8.40 m</text>
      </g>

      {/* North arrow */}
      <g className="pd-thin pd-fade" style={{ animationDelay: "1.3s" }}>
        <circle cx="474" cy="62" r="11" />
        <path d="M474 49L480 70L474 65L468 70Z" className="pd-solid" />
        <text x="474" y="44" className="pd-label-sm">N</text>
      </g>

      {/* Building */}
      <path d={WINDOWS} className="pd-thin pd-fade" style={{ animationDelay: "0.9s" }} />
      <path d={EXTERIOR} pathLength={1} className="pd-wall pd-draw" />
      <path d={INTERIOR} pathLength={1} className="pd-wall pd-wall-int pd-draw" style={{ animationDelay: "0.35s" }} />
      <path d={DOORS} className="pd-thin pd-fade" style={{ animationDelay: "1s" }} />
      <path d={FURNITURE} className="pd-thin pd-furniture pd-fade" style={{ animationDelay: "1.2s" }} />

      {/* Room labels */}
      <g className="pd-fade" style={{ animationDelay: "1.4s" }}>
        <text x="175" y="80" className="pd-label">LIVING</text>
        <text x="175" y="94" className="pd-area">32.1 m²</text>
        <text x="345" y="88" className="pd-label">KITCHEN</text>
        <text x="345" y="101" className="pd-area">18.8 m²</text>
        <text x="330" y="290" className="pd-label">BEDROOM</text>
        <text x="330" y="304" className="pd-area">29.1 m²</text>
        <text x="110" y="231" className="pd-label-sm">BATH</text>
        <text x="190" y="366" className="pd-label-sm">ENTRY</text>
        <path d="M190 352V334 M185 340L190 333L195 340" className="pd-thin" />
      </g>

      {/* Scale bar: 3 m */}
      <g className="pd-fade" style={{ animationDelay: "1.3s" }}>
        <path d="M340 346h33.3v5h-33.3z M406.6 346h33.4v5h-33.4z" className="pd-solid" />
        <path d="M340 346h100v5h-100z" className="pd-thin" />
        <text x="340" y="364" className="pd-label-sm">0</text>
        <text x="373.3" y="364" className="pd-label-sm">1</text>
        <text x="406.6" y="364" className="pd-label-sm">2</text>
        <text x="440" y="364" className="pd-label-sm">3 m</text>
      </g>

      {/* The walk */}
      <path d={LOOP} className="pd-trail pd-fade" style={{ animationDelay: "1.6s" }} />
      <g className="pd-fade" style={{ animationDelay: "1.8s" }}>
        <g className="pd-walker" transform={reduced ? "translate(200 200) rotate(-90)" : undefined}>
          <path d="M0 0L46 -20A50 50 0 0 1 46 20Z" className="pd-fov" />
          <circle r="11" className="pd-halo" />
          <circle r="5.5" className="pd-dot" />
          {!reduced && <animateMotion dur="16s" repeatCount="indefinite" rotate="auto" path={LOOP} />}
        </g>
      </g>
    </svg>
  );
}
