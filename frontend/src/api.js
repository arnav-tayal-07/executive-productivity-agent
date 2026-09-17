const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

export async function getBrief(asOf) {
  const res = await fetch(`${API_BASE}/brief?as_of=${asOf}`);
  if (!res.ok) throw new Error(`brief failed: ${res.status}`);
  return res.json();
}

export async function askQuestion(question, asOf) {
  const res = await fetch(`${API_BASE}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, as_of: asOf }),
  });
  if (!res.ok) throw new Error(`ask failed: ${res.status}`);
  return res.json();
}

export async function overrideTask(taskId, value, note, asOf) {
  const res = await fetch(`${API_BASE}/override`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId, value, note, as_of: asOf }),
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json()).detail || "";
    } catch {
      // response wasn't JSON — fall through with empty detail
    }
    throw new Error(`override failed (${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}
