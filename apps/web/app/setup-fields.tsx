"use client";

export type SetupProfile = {
  product_name: string;
  product_url: string;
  description: string;
  audience: string;
  goal: string;
  content_expectations: string;
  strategy: string;
  platform: string;
};
export type SetupTemplate = {
  version: number;
  body: {
    steps: string[];
    fields: Record<
      string,
      {
        label: string;
        placeholder: string;
        help: string;
        required: boolean;
        default: string;
      }
    >;
    strategies: { name: string; description: string }[];
    default_platform: string;
  };
};
export function initialProfile(
  template: SetupTemplate,
  initial = "",
): SetupProfile {
  const fields = template.body.fields;
  const profile: SetupProfile = {
    product_name: fields.product_name.default,
    product_url: fields.product_url.default,
    description: fields.description.default,
    audience: fields.audience.default,
    goal: fields.goal.default,
    content_expectations: fields.content_expectations.default,
    strategy:
      template.body.strategies[0].name +
      "：" +
      template.body.strategies[0].description,
    platform: template.body.default_platform,
  };
  if (initial)
    profile[/^https?:\/\/\S+$/.test(initial) ? "product_url" : "description"] =
      initial;
  return profile;
}

export default function SetupFields({
  step,
  template,
  profile,
  onChange,
  validate = true,
}: {
  step: number;
  template: SetupTemplate;
  profile: SetupProfile;
  onChange: (key: keyof SetupProfile, value: string) => void;
  validate?: boolean;
}) {
  const keys =
    step === 0
      ? ["product_name", "product_url", "description", "audience"]
      : ["goal", "content_expectations"];
  return (
    <>
      {keys.map((key) => {
        const field = template.body.fields[key];
        const props = {
          required: validate && field.required,
          placeholder: field.placeholder,
          value: profile[key as keyof SetupProfile],
          onChange: (
            e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
          ) => onChange(key as keyof SetupProfile, e.target.value),
          maxLength:
            key === "product_name" ? 80 : key === "description" ? 10000 : 2000,
        };
        return (
          <label key={key}>
            {field.label}
            {!field.required && <span className="muted"> 选填</span>}
            {key === "product_name" || key === "product_url" ? (
              <input
                {...props}
                type={key === "product_url" && validate ? "url" : "text"}
              />
            ) : (
              <textarea
                {...props}
                rows={
                  key === "description" || key === "content_expectations"
                    ? 3
                    : 2
                }
              />
            )}
            {field.help && <small className="muted">{field.help}</small>}
          </label>
        );
      })}
      {step === 1 && (
        <fieldset className="strategy-options">
          <legend>运营策略倾向</legend>
          {template.body.strategies.map((strategy) => (
            <label key={strategy.name}>
              <input
                type="radio"
                name="strategy"
                value={strategy.name}
                checked={
                  profile.strategy ===
                  strategy.name + "：" + strategy.description
                }
                onChange={() =>
                  onChange(
                    "strategy",
                    strategy.name + "：" + strategy.description,
                  )
                }
              />
              <span>
                <strong>{strategy.name}</strong>
                <small>{strategy.description}</small>
              </span>
            </label>
          ))}
        </fieldset>
      )}
    </>
  );
}
