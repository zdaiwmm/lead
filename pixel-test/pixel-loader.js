/* Installation runs in <head> on every visit. No code is loaded until you save it. */
(() => {
  "use strict";
  const boot = window.pixelBoot = { state: "empty", errors: [] };
  const report = message => {
    boot.errors.push(message);
    window.dispatchEvent(new CustomEvent("pixel:loader-error", { detail: message }));
  };
  addEventListener("error", event => {
    if (event.target instanceof HTMLScriptElement) report("脚本加载失败：" + event.target.src);
    else if (event.message) report(event.message);
  }, true);
  addEventListener("unhandledrejection", event => report(String(event.reason?.message || event.reason)));
  let code;
  try { code = localStorage.getItem("pixel-playground:code"); }
  catch { report("浏览器禁止本地存储，安装代码无法保存。"); }
  if (!code?.trim()) return;
  boot.state = "loading";
  boot.promise = (async () => {
    const nodes = /<script[\s>]/i.test(code)
      ? [...new DOMParser().parseFromString(code, "text/html").querySelectorAll("script")]
      : [Object.assign(document.createElement("script"), { textContent: code })];
    for (const node of nodes) {
      const script = document.createElement("script");
      for (const attribute of node.attributes) script.setAttribute(attribute.name, attribute.value);
      script.textContent = node.textContent;
      const loaded = script.src || script.type === "module" ? new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(Error("脚本加载超时")), 15000);
        script.addEventListener("load", () => { clearTimeout(timeout); resolve(); }, { once: true });
        script.addEventListener("error", () => { clearTimeout(timeout); reject(Error("脚本未能加载")); }, { once: true });
      }) : null;
      document.head.append(script);
      if (loaded) await loaded;
    }
    boot.state = boot.errors.length ? "error" : "executed";
  })().catch(error => { boot.state = "error"; report(error.message); });
})();
