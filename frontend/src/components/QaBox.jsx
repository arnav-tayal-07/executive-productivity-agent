import { useState } from "react";

const SUGGESTIONS = [
  "What did I promise Raghav?",
  "What needs action today?",
  "What did I promise Priya?",
];

export default function QaBox({ onAsk }) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("Ask a question about Arjun's commitments above.");
  const [provider, setProvider] = useState(null);
  const [loading, setLoading] = useState(false);

  const submit = async (q) => {
    const text = q ?? question;
    if (!text.trim()) return;
    setLoading(true);
    setAnswer("Thinking…");
    setProvider(null);
    try {
      const result = await onAsk(text);
      setAnswer(result.answer);
      setProvider(result.llm_provider || null);
    } catch (e) {
      setAnswer("Something went wrong reaching the agent.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="qa">
      <h2>Ask the agent</h2>
      <div className="qa-row">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder='e.g. "What did I promise Raghav?"'
        />
        <button className="btn btn-primary" onClick={() => submit()} disabled={loading}>
          Ask
        </button>
      </div>
      <div className="qa-suggestions">
        {SUGGESTIONS.map((s) => (
          <button key={s} className="btn btn-ghost btn-sm" onClick={() => { setQuestion(s); submit(s); }}>
            {s}
          </button>
        ))}
      </div>
      <div className="qa-answer">
        {answer}
        {provider && <div className="qa-provider">answered by {provider}</div>}
        {!provider && !loading && answer && answer !== "Ask a question about Arjun's commitments above." && answer !== "Thinking…" && (
          <div className="qa-provider">answered offline (keyword fallback — no LLM reachable)</div>
        )}
      </div>
    </div>
  );
}
