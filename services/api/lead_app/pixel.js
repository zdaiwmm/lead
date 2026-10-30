(() => {
  "use strict";
  const script = document.currentScript;
  if (!script) return;
  const cid = script.dataset.connection,
    key = script.dataset.key;
  if (!cid || !key) return;
  window.__leadPixels = window.__leadPixels || {};
  if (window.__leadPixels[cid]) return;
  window.__leadPixels[cid] = true;
  const base = new URL(script.src).origin,
    prefix = "lead:" + cid + ":";
  const memory = {},
    nativeReplace = history.replaceState.bind(history);
  const storage = (kind) => {
    try {
      return window[kind];
    } catch {
      return null;
    }
  };
  const persistent = storage("localStorage"),
    temporary = storage("sessionStorage");
  const read = (store, name) => {
    try {
      return store.getItem(prefix + name);
    } catch {
      return memory[name];
    }
  };
  const write = (store, name, value) => {
    memory[name] = value;
    try {
      store.setItem(prefix + name, value);
    } catch {}
  };
  const json = (name) => {
    try {
      return JSON.parse(read(temporary, name) || "null");
    } catch {
      return null;
    }
  };
  const uuid = () => crypto.randomUUID();
  let anonymous = read(persistent, "did") || uuid();
  write(persistent, "did", anonymous);
  let user = read(temporary, "user") || null;
  let testing = json("test"),
    testIdentity = json("test-identity"),
    terminal = read(temporary, "test-blocked") || "inactive";
  let context = null,
    chain = Promise.resolve(),
    pageUrl = "",
    active = 0,
    engagementSent = false,
    seconds = 10,
    checking = false;
  const queued = Array.from(window.leadq?.queue || []);
  function captureTest() {
    const code = new URL(location.href).searchParams.get("lead_test");
    if (code && code !== testing?.code) {
      testing = { code };
      terminal = "active";
      context = null;
      testIdentity = {
        code,
        anonymous: uuid(),
        user: null,
        session: uuid(),
        at: Date.now(),
      };
      write(temporary, "test", JSON.stringify(testing));
      write(temporary, "test-identity", JSON.stringify(testIdentity));
      write(temporary, "test-blocked", "");
    }
    if (testing && (!testIdentity || testIdentity.code !== testing.code)) {
      testIdentity = {
        code: testing.code,
        anonymous: uuid(),
        user: null,
        session: uuid(),
        at: Date.now(),
      };
      write(temporary, "test-identity", JSON.stringify(testIdentity));
    }
  }
  captureTest();
  function notifyTest() {
    if (typeof CustomEvent === "function")
      window.dispatchEvent?.(
        new CustomEvent("lead:test-state", { detail: testStatus() }),
      );
  }
  function testStatus() {
    return {
      state: testing ? "active" : terminal,
      id: testing?.id || null,
      expires_at: testing?.expires_at || null,
    };
  }
  function clearTest(state) {
    const wasTesting = Boolean(testing);
    testing = null;
    testIdentity = null;
    context = null;
    terminal = state;
    active = 0;
    engagementSent = true;
    write(temporary, "test", "");
    write(temporary, "test-identity", "");
    write(temporary, "test-ref", "");
    write(
      temporary,
      "test-blocked",
      ["expired", "invalid"].includes(state) ? state : "",
    );
    const url = new URL(location.href);
    if (wasTesting || url.searchParams.has("lead_test")) {
      url.searchParams.delete("lead_test");
      url.searchParams.delete("lr");
      url.searchParams.delete("lead_ref");
      nativeReplace(history.state, "", url.href);
      pageUrl = url.href;
    }
    notifyTest();
  }
  function assertNotExpired() {
    if (["expired", "invalid"].includes(terminal) && !testing)
      throw Error("lead: 测试已过期或无效，请重新开始测试或明确结束测试后继续");
  }
  function session() {
    if (testing) {
      if (Date.now() - testIdentity.at > 1800000) testIdentity.session = uuid();
      testIdentity.at = Date.now();
      write(temporary, "test-identity", JSON.stringify(testIdentity));
      return testIdentity.session;
    }
    let value;
    try {
      value = JSON.parse(read(persistent, "session") || "null");
    } catch {}
    if (!value || Date.now() - value.at > 1800000)
      value = { id: uuid(), at: Date.now() };
    value.at = Date.now();
    write(persistent, "session", JSON.stringify(value));
    return value.id;
  }
  function reference() {
    const query = new URL(location.href).searchParams,
      value = query.get("lr") || query.get("lead_ref");
    // Test navigation must not replace a real visitor's remembered source.
    const name = testing ? "test-ref" : "ref";
    if (value) write(temporary, name, value);
    return value || read(temporary, name) || null;
  }
  function snapshot() {
    assertNotExpired();
    return {
      ...context,
      connection_id: cid,
      session_id: session(),
      anonymous_id: testing ? testIdentity.anonymous : anonymous,
      touchpoint_id: context?.touchpoint_id || null,
      test_code: testing?.code || null,
    };
  }
  async function control(action = "status") {
    if (!testing) {
      if (action === "end") {
        clearTest("ended");
        chain = chain.catch(() => {});
      }
      return testStatus();
    }
    const code = testing.code;
    const response = await fetch(base + "/collect/" + cid + "/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "omit",
      body: JSON.stringify({
        key,
        url: location.href,
        test_code: code,
        action,
      }),
    });
    if (!response.ok)
      throw Error("lead: 无法核对测试会话 (" + response.status + ")");
    const result = await response.json();
    if (testing?.code === code) {
      if (!result.active) clearTest(result.state);
      else {
        testing = { ...testing, id: result.id, expires_at: result.expires_at };
        write(temporary, "test", JSON.stringify(testing));
        notifyTest();
      }
    }
    return result;
  }
  async function checkTest() {
    if (!testing || checking) return;
    checking = true;
    try {
      await control();
    } catch {
    } finally {
      checking = false;
    }
  }
  async function send(body) {
    let response;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        response = await fetch(base + "/collect/" + cid, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          keepalive: true,
          credentials: "omit",
        });
        break;
      } catch (error) {
        if (attempt) throw error;
      }
    }
    if (!response.ok) {
      let detail;
      try {
        detail = (await response.json()).detail;
      } catch {}
      if (
        detail?.code?.startsWith("pixel_test_") &&
        testing?.code === body.test_code
      )
        clearTest(detail.code.slice(11));
      throw Error(
        "lead: " +
          (detail?.message ||
            (typeof detail === "string"
              ? detail
              : "事件未被接收 (" + response.status + ")")),
      );
    }
    const result = await response.json();
    // An older in-flight request must not restore a test snapshot after termination.
    if ((body.test_code || null) === (testing?.code || null))
      context = result.attribution || context;
    seconds = Math.max(
      10,
      Math.min(120, Number(result.engagement_seconds) || 10),
    );
    return result;
  }
  function track(name, properties = {}, options = {}) {
    captureTest();
    try {
      assertNotExpired();
    } catch (error) {
      return Promise.reject(error);
    }
    const body = {
      key,
      event_name: name,
      event_id: options.event_id || uuid(),
      anonymous_id: testing ? testIdentity.anonymous : anonymous,
      session_id: session(),
      user_id: testing ? testIdentity.user : user,
      occurred_at: options.occurred_at || new Date().toISOString(),
      url: location.href,
      ref: reference(),
      test_code: options.test_code || testing?.code || null,
      properties,
    };
    const task = chain.catch(() => {}).then(() => send(body));
    chain = task;
    task.catch(() => {});
    return task;
  }
  function pageview() {
    pageUrl = location.href;
    active = 0;
    engagementSent = false;
    return track("PageView");
  }
  function leadq(command, ...args) {
    if (command === "track")
      return args[0] === "PageView" ? pageview() : track(...args);
    if (command === "identify") {
      captureTest();
      try {
        assertNotExpired();
      } catch (error) {
        return Promise.reject(error);
      }
      const value = String(args[0] ?? "");
      if (!value || value.length > 128)
        return Promise.reject(Error("lead: 用户编号无效"));
      if (testing) {
        testIdentity.user = value;
        write(temporary, "test-identity", JSON.stringify(testIdentity));
      } else {
        user = value;
        write(temporary, "user", user);
      }
      return track("Identify");
    }
    if (command === "attribution") return chain.then(snapshot);
    if (command === "ready") return chain.then(() => args[0](snapshot()));
    if (command === "testStatus") return checkTest().then(testStatus);
    if (command === "endTest")
      return chain
        .catch(() => {})
        .then(() => control("end"))
        .then((result) => {
          chain = Promise.resolve();
          return result;
        });
    if (command === "reset") {
      try {
        assertNotExpired();
      } catch (error) {
        return Promise.reject(error);
      }
      if (testing) {
        testIdentity = {
          code: testing.code,
          anonymous: uuid(),
          user: null,
          session: uuid(),
          at: Date.now(),
        };
        write(temporary, "test-identity", JSON.stringify(testIdentity));
      } else {
        anonymous = uuid();
        user = null;
        write(persistent, "did", anonymous);
        write(persistent, "session", "");
        write(temporary, "user", "");
        write(temporary, "ref", "");
      }
      context = null;
      return pageview();
    }
    return Promise.reject(Error("lead: 不支持的命令"));
  }
  leadq.version = "2.1";
  window.leadq = leadq;
  window.leadAttribution = snapshot;
  pageview().catch(() => {});
  checkTest();
  for (const item of queued) {
    if (item.args && item.resolve)
      Promise.resolve(leadq(...item.args)).then(item.resolve, item.reject);
    else leadq(...item);
  }
  for (const method of ["pushState", "replaceState"]) {
    const original = history[method];
    history[method] = function (...args) {
      const result = original.apply(this, args);
      if (location.href !== pageUrl) pageview().catch(() => {});
      return result;
    };
  }
  addEventListener("popstate", () => {
    if (location.href !== pageUrl) pageview().catch(() => {});
  });
  addEventListener("focus", checkTest);
  addEventListener("visibilitychange", () => {
    if (!document.hidden) checkTest();
  });
  setInterval(checkTest, 3000);
  setInterval(() => {
    if (!document.hidden && document.hasFocus()) active++;
    if (!engagementSent && active >= seconds) {
      engagementSent = true;
      track("Engagement").catch(() => {});
    }
  }, 1000);
})();
