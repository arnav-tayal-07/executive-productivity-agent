import TaskCard from "./TaskCard";

export default function BriefColumn({ title, tasks, onOverride, accentClass }) {
  return (
    <section className="column">
      <h2 className={accentClass}>
        {title} <span className="count">({tasks.length})</span>
      </h2>
      {tasks.length === 0 ? (
        <div className="empty">Nothing here.</div>
      ) : (
        tasks.map((t) => <TaskCard key={t.id} task={t} onOverride={onOverride} />)
      )}
    </section>
  );
}
