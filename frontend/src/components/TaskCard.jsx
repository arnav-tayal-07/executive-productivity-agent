import { label } from "../people";

export default function TaskCard({ task, onOverride }) {
  const {
    id, action, status, deadline_date, bucket,
    actor, recipient, candidate_owners, user_override, override_note,
  } = task;

  const ownerLine =
    bucket === "unclear_ownership" ? (
      <div className="tc-candidates">
        Considered: {candidate_owners.map(label).join(", ") || "—"} (none confirmed)
      </div>
    ) : (
      <div className="tc-meta-line">
        {bucket === "my_actions" ? "Owed to" : "Owner"}:{" "}
        <strong>{label(bucket === "my_actions" ? recipient || "—" : actor)}</strong>
      </div>
    );

  const showOverrideBtn = status !== "done" || user_override;

  return (
    <div className="task-card">
      <div className="tc-action">{action}</div>
      <div className="tc-meta">
        <span className={`badge badge-${status}`}>{status}</span>
        <span className="tc-deadline">Deadline: {deadline_date || "unspecified"}</span>
      </div>
      {ownerLine}
      {override_note && (
        <div className="tc-override-note">
          Manually marked {user_override} — {override_note}
        </div>
      )}
      {showOverrideBtn && (
        <div className="tc-override-row">
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => onOverride(id, user_override === "done" ? "not_done" : "done")}
          >
            {user_override === "done" ? "Undo manual confirm" : "Mark done manually"}
          </button>
        </div>
      )}
    </div>
  );
}
