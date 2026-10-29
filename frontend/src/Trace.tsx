import { useState } from "react";
import type { TaskView } from "./types";
import { argHint, elapsed, shortTime, skillVerb } from "./lib/format";
import Markdown from "./lib/markdown";

interface TraceProps {
  task: TaskView;
  rawReasoning: boolean;
  onOption?: (option: string) => void;
}

function StatusDot({ status }: { status: string }) {
  return <span className={`dot dot-${status}`} aria-hidden />;
}

function ThinkingBlock({
  text,
  ts,
  raw,
  defaultOpen,
}: {
  text: string;
  ts: number;
  raw?: boolean;
  defaultOpen?: boolean;
}) {
  const label = raw ? "reasoning" : "thinking";
  return (
    <details className={`thinking ${raw ? "thinking-raw" : ""}`} open={defaultOpen}>
      <summary>
        <span className="chev">›</span>
        <span className="thinking-label">{label}</span>
        <span className="thinking-preview">{text.replace(/\s+/g, " ").slice(0, 110)}</span>
        <span className="trace-time">{shortTime(ts * 1000)}</span>
      </summary>
      <pre className="thinking-body">{text}</pre>
    </details>
  );
}

function StepCard({ step }: { step: TaskView["steps"][number] }) {
  const hint = argHint(step.args);
  const argsJson = JSON.stringify(step.args, null, 2);
  const hasBody = Boolean(step.output || step.warning || argsJson !== "{}");
  return (
    <div className={`step step-${step.status}`}>
      <div className="step-head">
        <StatusDot status={step.status} />
        <span className="step-skill">{step.skill}</span>
        {hint && <span className="step-hint">{hint}</span>}
        <span className="step-spacer" />
        {step.elapsedMs ? (
          <span className="trace-time">{(step.elapsedMs / 1000).toFixed(1)}s</span>
        ) : null}
        <span className="trace-time">{shortTime(step.startedAt)}</span>
      </div>
      {step.why ? <div className="step-why">{step.why}</div> : null}
      {hasBody && (
        <details className="step-body">
          <summary>details</summary>
          {argsJson !== "{}" && (
            <>
              <div className="detail-label">input</div>
              <pre className="step-output">{argsJson}</pre>
            </>
          )}
          {step.output ? (
            <>
              <div className="detail-label">output</div>
              <pre className="step-output">{step.output}</pre>
            </>
          ) : null}
          {step.warning ? <div className="step-warning">⚠ {step.warning}</div> : null}
        </details>
      )}
    </div>
  );
}

function sourceLinks(evidence: Record<string, unknown> | undefined): string[] {
  if (!evidence) return [];
  const out: string[] = [];
  for (const value of Object.values(evidence)) {
    if (typeof value !== "string") continue;
    const match = value.match(/https?:\/\/[^\s<>"']+/g);
    if (match) out.push(...match);
  }
  return [...new Set(out)].slice(0, 6);
}

function ClarifyCard({
  task,
  onOption,
}: {
  task: TaskView;
  onOption?: (option: string) => void;
}) {
  const options = task.options || [];
  return (
    <div className="clarify">
      <div className="clarify-head">
        <StatusDot status="running" />
        <span>needs your input</span>
        <span className="step-spacer" />
        <span className="trace-time">{shortTime(task.updatedAt)}</span>
      </div>
      <div className="clarify-question">
        <Markdown text={task.question || task.goal} />
      </div>
      {options.length ? (
        <div className="clarify-options">
          {options.map((option, index) => (
            <button
              key={`${index}-${option}`}
              className="option-btn"
              onClick={() => onOption?.(option)}
              title={option}
            >
              <span className="option-index">{index + 1}</span>
              <span className="option-text">{option}</span>
            </button>
          ))}
        </div>
      ) : null}
      <div className="clarify-hint">
        {options.length
          ? "Pick an option, or type a different answer below."
          : "Type your answer below to continue."}
      </div>
    </div>
  );
}

function AnswerCard({ task }: { task: TaskView }) {
  const [copied, setCopied] = useState(false);
  const sources = sourceLinks(task.evidence);

  const copy = async () => {
    const text = task.answer || "";
    const markCopied = () => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    };
    try {
      await navigator.clipboard.writeText(text);
      markCopied();
    } catch {
      // Fallback for contexts where the async clipboard is unavailable.
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        if (document.execCommand("copy")) markCopied();
      } catch {
        // The text remains selectable by hand.
      }
      document.body.removeChild(area);
    }
  };

  return (
    <div className="answer">
      <div className="answer-head">
        <StatusDot status={task.live ? "running" : task.status} />
        <span>result</span>
        <span className="step-spacer" />
        <button className="answer-copy" onClick={copy} title="Copy the full answer">
          {copied ? "copied" : "copy"}
        </button>
        <span className="trace-time">{elapsed(task)}</span>
      </div>
      <div className="answer-body">
        <Markdown text={task.answer} />
        {task.live ? <span className="caret" /> : null}
      </div>
      {sources.length ? (
        <div className="answer-sources">
          <span className="detail-label">sources</span>
          {sources.map((url) => (
            <a key={url} href={url} target="_blank" rel="noreferrer" className="source-chip">
              {url.replace(/^https?:\/\//, "").replace(/\/$/, "").slice(0, 56)}
            </a>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default function Trace({ task, rawReasoning, onOption }: TraceProps) {
  const summaries = task.thinking.filter((b) => b.kind === "summary");
  const raws = rawReasoning ? task.thinking.filter((b) => b.kind === "raw") : [];
  const lastSummary = summaries[summaries.length - 1];

  return (
    <div className="trace">
      {summaries.map((block) => (
        <ThinkingBlock
          key={block.id}
          text={block.text}
          ts={block.ts}
          defaultOpen={block === lastSummary}
        />
      ))}
      {raws.map((block) => (
        <ThinkingBlock key={block.id} text={block.text} ts={block.ts} raw />
      ))}

      {task.steps.map((step) => (
        <StepCard key={step.id} step={step} />
      ))}

      {task.error ? <div className="error-box">{task.error}</div> : null}

      {task.needsInput ? (
        <ClarifyCard task={task} onOption={onOption} />
      ) : task.answer ? (
        <AnswerCard task={task} />
      ) : null}

      {!task.needsInput &&
      !task.answer &&
      !task.steps.length &&
      !summaries.length &&
      task.live ? (
        <div className="trace-empty">
          <span className="pulse" /> {skillVerb()}…
        </div>
      ) : null}
    </div>
  );
}
