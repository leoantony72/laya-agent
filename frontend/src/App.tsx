import { useEffect } from "react";
import Dashboard from "./Dashboard";
import Island from "./Island";
import { useAgent } from "./lib/useAgent";

export default function App() {
  const win =
    new URLSearchParams(window.location.search).get("win") || "dashboard";
  const agent = useAgent();

  useEffect(() => {
    document.documentElement.dataset.win = win;
    document.title = win === "island" ? "WinBrow Island" : "WinBrow";
  }, [win]);

  return win === "island" ? <Island agent={agent} /> : <Dashboard agent={agent} />;
}
