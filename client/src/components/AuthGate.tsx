import { useEffect, useState, type ReactNode } from "react";
import { fetchMe, logout, redirectToLogin, type AuthUser } from "../api";
import { LoadingNote } from "./LoadingNote";

type GateState =
  | { kind: "loading" }
  | { kind: "ready"; user: AuthUser }
  | { kind: "unreachable"; message: string };

interface AuthGateProps {
  children: (user: AuthUser, logOut: () => void) => ReactNode;
}

function logOut(): void {
  logout().catch(() => undefined).finally(redirectToLogin);
}

export function AuthGate({ children }: AuthGateProps) {
  const [state, setState] = useState<GateState>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    fetchMe()
      .then((user) => {
        if (cancelled) return;
        if (user === null) {
          redirectToLogin();
          return;
        }
        setState({ kind: "ready", user });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setState({
          kind: "unreachable",
          message: e instanceof Error ? e.message : String(e),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  function retry() {
    setState({ kind: "loading" });
    setAttempt((n) => n + 1);
  }

  if (state.kind === "ready") {
    return <>{children(state.user, logOut)}</>;
  }

  return (
    <main className="page">
      <header className="page-header">
        <h1>Pharmakon</h1>
      </header>
      <section className="panel">
        {state.kind === "loading" ? (
          <LoadingNote label="Checking your session…" />
        ) : (
          <>
            <h2>Can't reach the server</h2>
            <p className="error">{state.message}</p>
            <button type="button" className="control button" onClick={retry}>
              Try again
            </button>
          </>
        )}
      </section>
      <footer className="page-footer">
        <small>Educational and demonstrative use only. Not a medical device.</small>
      </footer>
    </main>
  );
}
