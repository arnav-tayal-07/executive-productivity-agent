import { useEffect, useState, useCallback } from "react";
import { getBrief, askQuestion, overrideTask } from "./api";
import BriefColumn from "./components/BriefColumn";
import QaBox from "./components/QaBox";
import "./App.css";

const DAYS = [
  { value: "2026-09-21", label: "Mon 21 Sep" },
  { value: "2026-09-22", label: "Tue 22 Sep" },
  { value: "2026-09-23", label: "Wed 23 Sep" },
  { value: "2026-09-24", label: "Thu 24 Sep" },
  { value: "2026-09-25", label: "Fri 25 Sep" },
];

export default function App() {
  const [asOf, setAsOf] = useState("2026-09-24");
  const [brief, setBrief] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (day) => {
    setLoading(true);
    setError(null);
    try {
      const data = await getBrief(day);
      setBrief(data);
    } catch (e) {
      setError("Couldn't reach the agent backend. Is it running on port 8000?");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(asOf);
  }, [asOf, load]);

  const handleOverride = async (taskId, value) => {
    try {
      await overrideTask(taskId, value, "confirmed by Arjun, no textual evidence found", asOf);
      await load(asOf);
    } catch (e) {
      // surface it instead of failing silently — a click that does nothing
      // visible is indistinguishable from a broken button
      setError(`Couldn't update that task: ${e.message}`);
    }
  };

  const handleAsk = (question) => askQuestion(question, asOf);

  return (
    <div className="app">
      <header>
        <div>
          <h1>Executive Productivity Agent</h1>
          <div className="sub">Daily brief for Arjun Malhotra (VP Sales)</div>
        </div>
        <div className="controls">
          <label htmlFor="asOf">View as of:</label>
          <select id="asOf" value={asOf} onChange={(e) => setAsOf(e.target.value)}>
            {DAYS.map((d) => (
              <option key={d.value} value={d.value}>{d.label}</option>
            ))}
          </select>
          <button className="btn btn-ghost" onClick={() => load(asOf)}>Refresh</button>
        </div>
      </header>

      <main>
        {error && <div className="error-banner">{error}</div>}
        {loading && !brief && <div className="loading">Loading brief…</div>}

        {brief && (
          <>
            <div className="columns">
              <BriefColumn
                title="My Actions"
                tasks={brief.my_actions}
                onOverride={handleOverride}
                accentClass="accent-mine"
              />
              <BriefColumn
                title="Waiting on Others"
                tasks={brief.waiting_on_others}
                onOverride={handleOverride}
                accentClass="accent-waiting"
              />
              <BriefColumn
                title="Unclear Ownership"
                tasks={brief.unclear_ownership}
                onOverride={handleOverride}
                accentClass="accent-unclear"
              />
            </div>
            <QaBox onAsk={handleAsk} />
          </>
        )}
      </main>
    </div>
  );
}
