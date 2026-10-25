import { useId, useState } from "react";
import "./why.css";
import type { WhyExplanation } from "../types/contract";

const PHONE_QUERY = "(max-width: 640px)";

function startsOpen(): boolean {
  return !window.matchMedia(PHONE_QUERY).matches;
}

export function WhyThisMatters({ why }: { why: WhyExplanation }) {
  const [open, setOpen] = useState(startsOpen);
  const bodyId = useId();
  const could = why.what_could_happen;
  const matters = why.why_it_matters;
  return (
    <div className="why">
      <button
        type="button"
        className="why-toggle"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen(!open)}
      >
        Why this matters
      </button>
      {open && (
        <div className="why-body" id={bodyId}>
          <p className="why-part">What this means</p>
          <p>{why.what_this_means}</p>
          {could && (
            <>
              <p className="why-part">What could happen</p>
              <p className="why-quote">"{could.quote}"</p>
              <p className="why-source">
                {could.citation}{" "}
                <a href={could.source_url} target="_blank" rel="noreferrer">
                  Open on DailyMed
                </a>
              </p>
            </>
          )}
          {matters && (
            <>
              <p className="why-part">Why it matters</p>
              <p>{matters.text}</p>
              <p className="why-source">
                {matters.citation}{" "}
                <a href={matters.source_url} target="_blank" rel="noreferrer">
                  Open source
                </a>
              </p>
            </>
          )}
          <p>{why.closing}</p>
          {why.poison_control && (
            <a className="why-call" href="tel:18002221222">
              Call Poison Control
            </a>
          )}
        </div>
      )}
    </div>
  );
}
