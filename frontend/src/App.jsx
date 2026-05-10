import { useState } from "react";
import { Send, Database, Sparkles, AlertCircle } from "lucide-react";
import {
  BarChart,
  Bar,
  LineChart,
  Line,
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

const API_URL = import.meta.env.VITE_API_URL || "";

export default function App() {
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState(null);
  const [error, setError] = useState(null);

  async function ask() {
    if (!question.trim() || loading) return;
    setLoading(true);
    setError(null);
    setResponse(null);
    try {
      const res = await fetch(`${API_URL}/api/v1/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setResponse(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "32px 24px" }}>
      <Header />

      <div style={cardStyle}>
        <div style={{ display: "flex", gap: 12 }}>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ask()}
            placeholder="Ask about your data — e.g. Top 10 customers by revenue last quarter"
            style={inputStyle}
            disabled={loading}
          />
          <button onClick={ask} disabled={loading} style={buttonStyle}>
            {loading ? "Thinking…" : <><Send size={16} /> Ask</>}
          </button>
        </div>
      </div>

      {error && (
        <div style={{ ...cardStyle, borderColor: "#ff7373", color: "#ff9d9d" }}>
          <AlertCircle size={18} style={{ verticalAlign: "middle", marginRight: 8 }} />
          {error}
        </div>
      )}

      {response && <ResponseView response={response} />}
    </div>
  );
}

function Header() {
  return (
    <div style={{ marginBottom: 24 }}>
      <h1 style={{ display: "flex", alignItems: "center", gap: 10, margin: 0 }}>
        <Sparkles color="var(--accent)" />
        BI Copilot
      </h1>
      <p style={{ color: "var(--muted)", margin: "4px 0 0", fontSize: 14 }}>
        Multi-agent analytics over BigQuery · Planner → SQL → Viz → Narrator
      </p>
    </div>
  );
}

function ResponseView({ response }) {
  const { sql, data, columns, viz, narrative, follow_ups, metadata, error } = response;
  if (error) {
    return (
      <div style={{ ...cardStyle, borderColor: "#ff7373", color: "#ff9d9d" }}>
        <AlertCircle size={18} style={{ verticalAlign: "middle", marginRight: 8 }} />
        {error}
      </div>
    );
  }

  return (
    <>
      {narrative && (
        <div style={cardStyle}>
          <h2 style={{ marginTop: 0, fontSize: 22 }}>{narrative.headline}</h2>
          <p style={{ color: "var(--text)", lineHeight: 1.6 }}>{narrative.summary}</p>
          {narrative.key_insights?.length > 0 && (
            <ul style={{ paddingLeft: 18, color: "var(--muted)" }}>
              {narrative.key_insights.map((k, i) => <li key={i}>{k}</li>)}
            </ul>
          )}
        </div>
      )}

      {viz && data?.length > 0 && (
        <div style={cardStyle}>
          <ChartRenderer chart_type={viz.chart_type} data={data} columns={columns} />
        </div>
      )}

      {follow_ups?.length > 0 && (
        <div style={cardStyle}>
          <h3 style={{ marginTop: 0, fontSize: 14, color: "var(--muted)" }}>Follow-up questions</h3>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {follow_ups.map((q, i) => (
              <span key={i} style={chipStyle}>{q}</span>
            ))}
          </div>
        </div>
      )}

      <details style={cardStyle}>
        <summary style={{ cursor: "pointer", color: "var(--muted)" }}>
          <Database size={14} style={{ verticalAlign: "middle", marginRight: 6 }} />
          Generated SQL · {metadata.sql_attempts} attempt(s) · {metadata.total_latency_ms}ms · ${metadata.cost_usd}
        </summary>
        <pre style={preStyle}>{sql}</pre>
      </details>
    </>
  );
}

function ChartRenderer({ chart_type, data, columns }) {
  if (chart_type === "kpi") {
    const [col] = columns;
    const value = data[0]?.[col];
    return (
      <div style={{ textAlign: "center", padding: 24 }}>
        <div style={{ fontSize: 14, color: "var(--muted)" }}>{col}</div>
        <div style={{ fontSize: 48, fontWeight: 700, color: "var(--accent)" }}>
          {typeof value === "number" ? value.toLocaleString() : String(value)}
        </div>
      </div>
    );
  }

  if (chart_type === "table") {
    return (
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>{columns.map((c) => <th key={c} style={thStyle}>{c}</th>)}</tr>
          </thead>
          <tbody>
            {data.slice(0, 100).map((row, i) => (
              <tr key={i}>{columns.map((c) => <td key={c} style={tdStyle}>{String(row[c] ?? "")}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  const [xKey, ...yKeys] = columns;

  if (chart_type === "line") {
    return (
      <ResponsiveContainer width="100%" height={320}>
        <LineChart data={data}>
          <CartesianGrid stroke="#2a3148" strokeDasharray="3 3" />
          <XAxis dataKey={xKey} stroke="#8a90a8" />
          <YAxis stroke="#8a90a8" />
          <Tooltip contentStyle={{ background: "#141a2e", border: "1px solid #2a3148" }} />
          {yKeys.map((k, i) => (
            <Line key={k} dataKey={k} stroke={i === 0 ? "var(--accent)" : "var(--accent-2)"} strokeWidth={2} dot={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    );
  }

  if (chart_type === "scatter") {
    return (
      <ResponsiveContainer width="100%" height={320}>
        <ScatterChart>
          <CartesianGrid stroke="#2a3148" strokeDasharray="3 3" />
          <XAxis dataKey={xKey} stroke="#8a90a8" />
          <YAxis dataKey={yKeys[0]} stroke="#8a90a8" />
          <Tooltip contentStyle={{ background: "#141a2e", border: "1px solid #2a3148" }} />
          <Scatter data={data} fill="var(--accent)" />
        </ScatterChart>
      </ResponsiveContainer>
    );
  }

  // default: bar
  return (
    <ResponsiveContainer width="100%" height={320}>
      <BarChart data={data}>
        <CartesianGrid stroke="#2a3148" strokeDasharray="3 3" />
        <XAxis dataKey={xKey} stroke="#8a90a8" />
        <YAxis stroke="#8a90a8" />
        <Tooltip contentStyle={{ background: "#141a2e", border: "1px solid #2a3148" }} />
        <Bar dataKey={yKeys[0]} fill="var(--accent)" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

const cardStyle = {
  background: "var(--panel)",
  border: "1px solid var(--border)",
  borderRadius: 12,
  padding: 20,
  marginBottom: 16,
};
const inputStyle = {
  flex: 1,
  background: "#0b1020",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: "12px 14px",
  color: "var(--text)",
  fontSize: 14,
  outline: "none",
};
const buttonStyle = {
  background: "var(--accent)",
  border: "none",
  borderRadius: 8,
  padding: "0 18px",
  color: "white",
  fontWeight: 600,
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
};
const chipStyle = {
  background: "#1c2238",
  border: "1px solid var(--border)",
  borderRadius: 16,
  padding: "6px 12px",
  fontSize: 13,
  color: "var(--muted)",
};
const preStyle = {
  background: "#0b1020",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: 14,
  fontSize: 12,
  overflowX: "auto",
  color: "#c8cee0",
};
const thStyle = { textAlign: "left", padding: 8, borderBottom: "1px solid var(--border)", fontSize: 12, color: "var(--muted)" };
const tdStyle = { padding: 8, borderBottom: "1px solid var(--border)", fontSize: 13 };
