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
  let agent = null,
    capabilities = {},
    initialized = Promise.resolve();
  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  let context = null,
    chain = Promise.resolve(),
    attributionPending = false,
    pageGeneration = 0,
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
      url.searchParams.delete("lc");
      nativeReplace(history.state, "", url.href);
      pageUrl = url.href;
    }
    notifyTest();
  }
  function assertNotExpired() {
    if (["expired", "invalid"].includes(terminal) && !testing)
      throw Error("lead: 测试已过期或无效，请重新开始测试或明确结束测试后继续");
  }
  // Anonymous identity spans visits; the entry source belongs to one tab visit.
  const visitTimeout = 30 * 60 * 1000;
  let visit = json("visit-v2");
  let previousEntry;
  function readEntry() {
    const query = new URL(location.href).searchParams;
    return {
      ref: query.get("lr") || query.get("lead_ref"),
      token: query.get("lc"),
    };
  }
  function continuesDocument() {
    const navigation = window.performance?.getEntriesByType?.("navigation")[0]?.type;
    if (navigation === "reload") return true;
    if (navigation === "back_forward" || !document.referrer) return false;
    return new URL(document.referrer).origin === new URL(location.href).origin;
  }
  function updateVisit(page = false) {
    const now = Date.now();
    const fresh = visit && now - visit.at < visitTimeout;
    const firstDocument = !previousEntry;
    if (page || firstDocument) {
      const entry = readEntry();
      const tagged = Boolean(entry.ref || entry.token);
      const external = firstDocument && !continuesDocument();
      const previous = previousEntry || visit;
      const changed = entry.token !== previous?.token || entry.ref !== previous?.ref;
      const newPromotion = tagged && (external || changed);
      if (!fresh || external || newPromotion) {
        visit = { id: uuid(), ref: newPromotion ? entry.ref : null,
          token: newPromotion ? entry.token : null };
      }
      previousEntry = entry;
    } else if (!fresh) {
      visit = { id: uuid(), ref: null, token: null };
    }
    visit.at = now;
    write(temporary, "visit-v2", JSON.stringify(visit));
    return visit;
  }
  function session() {
    if (testing) {
      if (Date.now() - testIdentity.at > visitTimeout) testIdentity.session = uuid();
      testIdentity.at = Date.now();
      write(temporary, "test-identity", JSON.stringify(testIdentity));
      return testIdentity.session;
    }
    return updateVisit().id;
  }
  function clickProof() {
    if (testing || !visit?.token) return null;
    try {
      const proof = JSON.parse(
        atob(visit.token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")),
      );
      if (proof.exp * 1000 <= Date.now()) {
        visit.token = null;
        visit.ref = null;
        write(temporary, "visit-v2", JSON.stringify(visit));
        return null;
      }
      return { token: visit.token, id: proof.id };
    } catch {
      // The collector validates malformed proofs and never falls back to a marker.
      return { token: visit.token };
    }
  }
  function reference() {
    if (!testing) return visit?.ref || null;
    const value = readEntry().ref;
    if (value) write(temporary, "test-ref", value);
    return value || read(temporary, "test-ref") || null;
  }
  function snapshot() {
    assertNotExpired();
    if (!context || attributionPending)
      throw Error("lead: 归因尚未准备完成，请等待 attribution()");
    return {
      ...context,
      connection_id: cid,
      session_id: session(),
      anonymous_id: testing ? testIdentity.anonymous : anonymous,
      touchpoint_id: context?.touchpoint_id || null,
      test_code: testing?.code || null,
      click_id: context?.click_id || null,
      click_token:
        context?.click_id && context.click_id === clickProof()?.id
          ? clickProof()?.token
          : null,
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
  const safeUrl = () => {
    const url = new URL(location.href);
    url.search = "";
    url.hash = "";
    return url.href;
  };
  const cached = json("buffer");
  let buffer = (Array.isArray(cached) ? cached : [])
    .filter(
      (item) =>
        item &&
        Number.isFinite(item.at) &&
        item.at > Date.now() - 86400000 &&
        item.body &&
        typeof item.body.event_id === "string",
    )
    .slice(0, 200);
  const waiters = new Map();
  let flushing = false,
    scheduled = false;
  function persist() {
    write(temporary, "buffer", JSON.stringify(buffer));
  }
  persist();
  function schedule(ms = 100) {
    if (scheduled) return;
    scheduled = true;
    setTimeout(() => {
      scheduled = false;
      void flush();
    }, ms);
  }
  function enqueue(body) {
    // Never store the installation key; retries retain the original click proof.
    const { key: _key, click_token: proof, ...saved } = body;
    if (
      buffer.length >= 200 ||
      JSON.stringify(buffer).length + JSON.stringify(saved).length > 1024 * 1024
    )
      return Promise.reject(Error("lead: 待发送事件已满，请等待网络恢复"));
    const item = {
      at: Date.now(),
      body: saved,
      attempt: 0,
      next: 0,
      click: proof ? { token: proof } : null,
    };
    buffer.push(item);
    persist();
    schedule();
    if (buffer.length >= 20) void flush();
    return new Promise((resolve, reject) =>
      waiters.set(item, { resolve, reject }),
    );
  }
  function settle(item, result, error) {
    buffer = buffer.filter((x) => x !== item);
    const pending = waiters.get(item);
    waiters.delete(item);
    if (error) pending?.reject(error);
    else pending?.resolve(result);
  }
  async function flush() {
    if (flushing || !agent || !capabilities.batch || !buffer.length) return;
    flushing = true;
    try {
      const now = Date.now();
      for (const item of [...buffer])
        if (item.at < now - 86400000)
          settle(item, null, Error("lead: 待发送事件已过期"));
      const first = buffer.find((x) => (x.next || 0) <= now);
      if (!first) return;
      const group = [];
      let bytes = 0;
      for (const item of buffer) {
        const encoded = JSON.stringify(item.body);
        const size =
          typeof TextEncoder === "function"
            ? new TextEncoder().encode(encoded).length + 1200
            : encoded.length * 3 + 1200;
        if (
          (item.body.test_code || null) !== (first.body.test_code || null) ||
          (item.next || 0) > now
        )
          continue;
        if (group.length === 20 || bytes + size > 48000) break;
        group.push(item);
        bytes += size;
      }
      const code = first.body.test_code || null;
      const events = group.map((item) => ({
        ...item.body,
        click_token: item.click?.token || null,
      }));
      const response = await fetch(base + "/collect/" + cid + "/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "omit",
        keepalive: true,
        body: JSON.stringify({
          agent_id: agent,
          write_key: key,
          environment: code ? "test" : "production",
          test_code: code,
          events,
        }),
      });
      if (response.status === 429 || response.status >= 500) {
        const error = Error("retry");
        error.retry = Number(response.headers?.get("Retry-After")) || 0;
        throw error;
      }
      if (!response.ok) {
        for (const item of group)
          settle(
            item,
            null,
            Error("lead: 批次被拒绝 (" + response.status + ")"),
          );
      } else {
        const result = await response.json();
        if (result.agent_id !== agent) throw Error("lead: 数据归属不匹配");
        for (const item of group) {
          const receipt = result.events?.find(
            (x) => x.event_id === item.body.event_id,
          );
          if (!receipt) continue;
          if (
            receipt.status === "rejected" ||
            ["quarantined", "failed"].includes(receipt.status)
          )
            settle(
              item,
              null,
              Error("lead: " + (receipt.error_code || "事件被拒绝")),
            );
          else settle(item, receipt, null); // Durable acceptance, not a fabricated conversion result.
        }
      }
    } catch (error) {
      const now = Date.now();
      for (const item of buffer.filter((x) => (x.next || 0) <= now)) {
        item.attempt = (item.attempt || 0) + 1;
        item.next =
          now +
          Math.max(
            (error.retry || 0) * 1000,
            Math.min(60000, 1000 * 2 ** Math.min(item.attempt, 6)),
          ) *
            (1 + Math.random() * 0.2);
      }
    } finally {
      flushing = false;
      persist();
      if (buffer.length)
        schedule(
          Math.max(
            100,
            Math.min(
              ...buffer.map((x) => Math.max(0, (x.next || 0) - Date.now())),
            ),
          ),
        );
    }
  }
  async function processed(body, result) {
    if (
      result.attribution ||
      !capabilities.receipts ||
      result.status !== "pending"
    )
      return result;
    for (let attempt = 0; attempt < 8; attempt++) {
      await delay(Math.min(4000, 250 * 2 ** attempt));
      let response;
      try {
        response = await fetch(base + "/collect/" + cid + "/receipts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "omit",
          body: JSON.stringify({
            key,
            url: body.url,
            anonymous_id: body.anonymous_id,
            session_id: body.session_id,
            test_code: body.test_code,
            event_ids: [body.event_id],
          }),
        });
      } catch {
        continue;
      }
      if (response.status === 429) {
        await delay((Number(response.headers?.get("Retry-After")) || 1) * 1000);
        continue;
      }
      if (!response.ok) throw Error("lead: 无法取得归因回执");
      const event = (await response.json()).events?.[0];
      if (event?.status === "processed") return event.result;
      if (["failed", "quarantined", "unknown"].includes(event?.status))
        throw Error("lead: 归因回执不可用");
    }
    throw Error("lead: 归因仍在处理中，请稍后重试读取");
  }
  async function send(body) {
    if (body.event_name === "PageView") {
      const { key: _key, ...saved } = body;
      write(
        temporary,
        "bootstrap",
        JSON.stringify({ at: Date.now(), body: saved }),
      );
    }
    let response;
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        response = await fetch(base + "/collect/" + cid, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          keepalive: true,
          credentials: "omit",
        });
        if (
          (response.status === 429 || response.status >= 500) &&
          attempt < 5
        ) {
          await delay(
            Math.max(
              (Number(response.headers?.get("Retry-After")) || 0) * 1000,
              Math.min(30000, 1000 * 2 ** attempt),
            ),
          );
          continue;
        }
        break;
      } catch (error) {
        if (attempt === 5) throw error;
        if (typeof setTimeout === "function")
          await delay(Math.min(30000, 1000 * 2 ** attempt));
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
    let result = await response.json();
    if (result.agent_id) {
      if (agent && agent !== result.agent_id)
        throw Error("lead: 数据归属不匹配");
      agent = result.agent_id;
      capabilities = result.capabilities || capabilities;
      if (buffer.length) schedule();
    }
    result = await processed(body, result);
    if (
      body.event_name === "PageView" &&
      json("bootstrap")?.body?.event_id === body.event_id
    )
      write(temporary, "bootstrap", "");
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
      url: safeUrl(),
      click_token: clickProof()?.token || null,
      ref: reference(),
      test_code: options.test_code || testing?.code || null,
      properties,
    };
    const task =
      name === "PageView"
        ? chain.catch(() => {}).then(() => send(body))
        : initialized.then(() =>
            capabilities.batch ? enqueue(body) : send(body),
          );
    chain = Promise.all([chain.catch(() => {}), task]).then(() => undefined);
    if (name === "PageView") initialized = task;
    task.catch(() => {});
    return task;
  }
  function pageview() {
    pageUrl = location.href;
    active = 0;
    engagementSent = false;
    attributionPending = true;
    const generation = ++pageGeneration;
    captureTest();
    if (!testing) updateVisit(true);
    const task = track("PageView");
    task.then(
      () => {
        if (generation === pageGeneration) attributionPending = false;
      },
      () => {},
    );
    return task;
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
        visit = null;
        write(temporary, "visit-v2", "");
        write(temporary, "user", "");
      }
      context = null;
      return pageview();
    }
    return Promise.reject(Error("lead: 不支持的命令"));
  }
  leadq.version = "2.3";
  leadq.flush = flush;
  window.leadq = leadq;
  window.leadAttribution = snapshot;
  const previous = json("bootstrap");
  if (
    previous?.at > Date.now() - 86400000 &&
    previous.body?.event_name === "PageView"
  ) {
    chain = send({ ...previous.body, key });
    initialized = chain;
  }
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
  addEventListener("online", () => {
    for (const item of buffer) item.next = 0;
    void flush();
  });
  addEventListener("pagehide", () => {
    void flush();
  });
  addEventListener("visibilitychange", () => {
    if (!document.hidden) checkTest();
    if (document.hidden) void flush();
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
