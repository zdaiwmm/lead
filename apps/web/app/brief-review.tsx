"use client";

export type Brief = {
  product_url: string;
  product_name: string;
  introduction: string;
  features: string[];
  customers: string[];
  scenarios: string[];
  advantages: { text: string; source: string }[];
  notes: string;
  review_notice?: string;
};

export function emptyBrief(website = ""): Brief {
  return {
    product_url: website,
    product_name: "",
    introduction: "",
    features: [],
    customers: [],
    scenarios: [],
    advantages: [],
    notes: "",
  };
}

const groups = [
  ["features", "核心功能"],
  ["customers", "目标客户"],
  ["scenarios", "使用场景"],
  ["advantages", "产品优势"],
] as const;

export default function BriefReview({
  brief,
  edit,
}: {
  brief: Brief;
  edit: (patch: Partial<Brief>) => void;
}) {
  return (
    <>
      {brief.review_notice && (
        <p role="status" className="muted small">
          {brief.review_notice}
        </p>
      )}
      <label>
        产品名称
        <input
          maxLength={80}
          value={brief.product_name}
          onChange={(e) => edit({ product_name: e.target.value })}
        />
      </label>
      <label>
        一句话介绍
        <textarea
          rows={2}
          maxLength={2000}
          aria-label="一句话介绍"
          value={brief.introduction}
          onChange={(e) => edit({ introduction: e.target.value })}
        />
      </label>
      {groups.map(([key, title]) => {
        const lines =
          key === "advantages"
            ? brief.advantages.map((item) => item.text)
            : brief[key];
        return (
          <div key={key}>
            <label>
              {title}
              <textarea
                rows={2}
                aria-label={title}
                placeholder="多条内容可换行填写"
                value={lines.join("\n")}
                onChange={(e) => {
                  const values = e.target.value.split("\n");
                  if (key !== "advantages") return edit({ [key]: values });
                  // A source follows its original claim when lines are moved or removed.
                  const originals = [...brief.advantages];
                  edit({
                    advantages: values.map((text) => {
                      const index = originals.findIndex(
                        (item) => item.text.trim() === text.trim(),
                      );
                      return {
                        text,
                        source:
                          index < 0 ? "" : originals.splice(index, 1)[0].source,
                      };
                    }),
                  });
                }}
              />
            </label>
            {key === "advantages" &&
              brief.advantages.map(
                (item, i) =>
                  item.source && (
                    <small key={i} className="muted">
                      {" "}
                      <a href={item.source} target="_blank" rel="noreferrer">
                        第 {i + 1} 条出处 ↗
                      </a>{" "}
                    </small>
                  ),
              )}
          </div>
        );
      })}
      <label>
        补充说明
        <textarea
          rows={2}
          maxLength={2000}
          aria-label="补充说明"
          value={brief.notes}
          onChange={(e) => edit({ notes: e.target.value })}
        />
      </label>
    </>
  );
}
