export type Work = {
  id: string;
  task_title?: string;
  basis_task_id?: string;
  priority?: number;
  observed_today?: boolean;
  parent?: Work;
  manuscript_at?: string;
  observed_clicks?: number;
  observed_at?: string;
  placeholder?: boolean;
  materialOnly?: boolean;
  stage: string;
  state: string;
  title: string;
  platform?: string | null;
  reason?: string;
  next_step?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  scheduled_at?: string | null;
  channel_check_at?: string | null;
  completed_at?: string | null;
  completed_today?: boolean;
  carried_over?: boolean;
  receipt?: string | null;
  external_url?: string | null;
  evidence_task_id?: string;
  activity_task_id?: string | null;
  source_task_id?: string | null;
  synthetic?: boolean;
  latest_activity?: { title: string; at: string; level: string } | null;
  delivery?: { state?: string; [key: string]: unknown } | null;
};
export const stages: Record<string, string> = {
  audience: "需求研究",
  content: "内容准备",
  distribution: "渠道分发",
  observation: "效果观察",
};
export const lanes = [
  { key: "ready", title: "待执行" },
  { key: "running", title: "进行中" },
  { key: "done", title: "已完成" },
  { key: "waiting", title: "待条件" },
];
export function visibleWork(item: Work): Work {
  // A task marked succeeded without its external receipt is still unresolved.
  if (item.stage === "distribution" && item.state === "done" && !item.receipt)
    return { ...item, state: "reconciling", reason: "发布回执尚未确认" };
  return item;
}
export function laneFor(item: Work) {
  if (["running", "reconciling"].includes(item.state)) return "running";
  if (item.state === "done") return "done";
  if (item.state === "ready") return "ready";
  if (item.state === "canceled") return "canceled";
  return "waiting";
}
export function synthetic(item: Work) {
  return item.synthetic || item.id.startsWith("distribute:");
}
export function statusLabel(item: Work) {
  return (
    (
      {
        unrelated: "不适用",
        stopped: "未执行",
        basis: "有选题依据",
        noResearch: "未关联研究",
        beforePublication: "待发布后观察",
        beforeDistribution: "尚未安排",
        noFeedback: "暂无可归因数据",
        unlinked: "无关联记录",
        recorded: "已记录",
        ready: "未开始",
        running: "进行中",
        done: "已完成",
        reconciling: "核对回执",
        blocked: "受阻",
        canceled: "已取消",
        paused: "已暂停",
      } as Record<string, string>
    )[item.state] || "等待条件"
  );
}
export function timestamp(
  value: string | null | undefined,
  zone: string,
  date = true,
) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return "";
  return new Date(value).toLocaleString("zh-CN", {
    timeZone: zone,
    ...(date ? ({ month: "numeric", day: "numeric" } as const) : {}),
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
export function workNote(item: Work, zone: string, asOf?: string) {
  const when = timestamp(
    item.completed_at || item.latest_activity?.at || item.started_at,
    zone,
  );
  if (item.state === "done")
    return `${when} ${item.stage === "distribution" ? "回执已确认" : "执行已记录"}`.trim();
  if (
    item.scheduled_at &&
    new Date(item.scheduled_at).getTime() >
      new Date(asOf || Date.now()).getTime() &&
    ["ready", "waiting", "blocked"].includes(item.state)
  )
    return `${timestamp(item.scheduled_at, zone)} 后检查`;
  if (item.state === "running") return when ? `${when} 最近更新` : "正在执行";
  if (item.state === "reconciling")
    return when ? `${when} 最近核对` : "等待原动作回执";
  return (
    item.reason ||
    (synthetic(item)
      ? "等待分发条件"
      : item.carried_over
        ? "延续原任务"
        : item.state === "ready"
          ? "等待执行"
          : "等待执行条件")
  );
}

export function todayWork(items: Work[]) {
  return items
    .map(visibleWork)
    .filter(
      (item) =>
        item.state !== "done" || item.completed_today || item.observed_today,
    );
}

export type Chain = { current: Work; title: string; steps: Work[] };

const stageTitles: Record<string, string> = {
  content: "准备渠道内容",
  distribution: "发布渠道内容",
  audience: "研究受众需求",
  observation: "复盘运营效果",
};

function taskTitle(item: Work) {
  return item.task_title || stageTitles[item.stage] || "推进运营任务";
}

type ExecutionDay = { period: string; zone: string };

export function taskChains(queue: Work[], day?: ExecutionDay): Chain[] {
  const byId = new Map(queue.map((item) => [item.id, item]));
  const active = day ? queue.map(visibleWork) : todayWork(queue);
  const calendar = day && new Intl.DateTimeFormat("sv-SE", { timeZone: day.zone });
  function parentOf(item: Work) {
    if (synthetic(item)) return byId.get(item.evidence_task_id || "");
    return item.parent && (byId.get(item.parent.id) || item.parent);
  }
  const ancestors = new Set(active.map((item) => parentOf(item)?.id));
  const chains: Chain[] = active
    .filter((item) => !ancestors.has(item.id))
    .map((current) => {
      const parent = parentOf(current);
      const steps = new Map<string, Work>();
      if (parent) steps.set(parent.stage, parent);
      steps.set(current.stage, current);
      if (current.manuscript_at && !steps.has("content")) {
        steps.set("content", {
          ...current,
          id: "manuscript:" + current.id,
          stage: "content",
          state: "done",
          completed_at: current.manuscript_at,
          reason: "发布任务已保存定稿。",
          external_url: null,
          materialOnly: true,
        });
      }
      if (current.observed_clicks) {
        steps.set("observation", {
          ...current,
          id: "clicks:" + current.id,
          stage: "observation",
          state: "done",
          completed_at: current.observed_at,
          reason: `已记录 ${current.observed_clicks} 次有效推广点击`,
          evidence_task_id: current.id,
          activity_task_id: current.id,
          external_url: null,
        });
      }
      if (day && calendar) {
        const candidates = [...steps.values()];
        const visited = new Set([current.id]);
        let ancestor = parent;
        while (ancestor && !visited.has(ancestor.id)) {
          visited.add(ancestor.id);
          if (!candidates.some((item) => item.id === ancestor?.id)) candidates.unshift(ancestor);
          ancestor = parentOf(ancestor);
        }
        const visible = candidates.map(visibleWork).filter((item) => {
          if (laneFor(item) === "running") return true;
          if (item.state !== "done") return false;
          if (!item.completed_at) return item.completed_today === true;
          const at = Date.parse(item.completed_at);
          return Number.isFinite(at) && calendar.format(at) === day.period;
        });
        visible.sort((a, b) => Object.keys(stages).indexOf(a.stage) - Object.keys(stages).indexOf(b.stage));
        const ranked = [...visible].sort(compareExecution);
        const lead = ranked[0];
        if (!lead) return null;
        const title = lead.materialOnly || lead.id.startsWith("clicks:")
          ? stageTitles[lead.stage] : taskTitle(lead);
        return { current: lead, title, steps: visible };
      }
      const title = taskTitle(current);
      return {
        current,
        title,
        steps: Object.keys(stages).map((stage) => {
          const recorded = steps.get(stage);
          if (recorded) return recorded;
          const basisId = current.basis_task_id || parent?.basis_task_id;
          if (stage === "audience" && basisId)
            return {
              id: "basis:" + basisId,
              stage,
              title: "选题依据",
              state: "basis",
              basis_task_id: basisId,
              platform: current.platform,
            };
          let state = "unlinked",
            reason = "没有与本任务明确关联的阶段记录。";
          if (
            current.stage === "audience" ||
            (current.stage === "observation" && stage !== "audience")
          ) {
            state = "unrelated";
            reason = "这项独立任务没有安排此步骤。";
          } else if (stage === "audience") {
            state = "noResearch";
            reason =
              "没有关联独立的需求研究任务。已保存的稿件事实依据可在内容准备中查看。";
          } else if (current.state === "canceled") {
            state = "stopped";
            reason = "任务已取消，未发现此步骤的执行记录。";
          } else if (stage === "observation") {
            const published =
              current.stage === "distribution" &&
              current.state === "done" &&
              !!current.receipt;
            state = published ? "noFeedback" : "beforePublication";
            reason = published
              ? "已确认发布，尚无可归因到该内容的推广点击记录。"
              : "尚未确认公开发布，暂时无法观察这篇内容的推广效果。";
          } else if (stage === "distribution" && current.stage === "content") {
            state = "beforeDistribution";
            reason = "尚无与此稿件关联的分发安排。";
          }
          return {
            id: current.id + ":" + stage,
            stage,
            title,
            state,
            reason,
            placeholder: true,
            platform: current.platform,
          };
        }),
      };
    })
    .filter((chain): chain is Chain => chain !== null);
  if (!day) return chains.sort((a, b) => (b.current.priority || 0) - (a.current.priority || 0));
  // A hidden sibling may promote the same parent that another visible chain already contains.
  const supporting = new Set(chains.flatMap(({ current, steps }) =>
    steps.filter((item) => item.id !== current.id).map((item) => item.id)));
  const seen = new Set<string>();
  return chains.sort((a, b) => compareExecution(a.current, b.current)).filter(({ current, steps }) => {
    if (seen.has(current.id) || (steps.length === 1 && supporting.has(current.id))) return false;
    seen.add(current.id);
    return true;
  });
}

function compareExecution(a: Work, b: Work) {
  const runningA = laneFor(a) === "running", runningB = laneFor(b) === "running";
  if (runningA !== runningB) return runningA ? -1 : 1;
  return runningA ? (b.priority || 0) - (a.priority || 0)
    : (Date.parse(b.completed_at || "") || 0) - (Date.parse(a.completed_at || "") || 0);
}
