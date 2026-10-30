(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const storage = {
    get(key, fallback = "") {
      try {
        return localStorage.getItem("pixel-playground:" + key) ?? fallback;
      } catch {
        return fallback;
      }
    },
    set(key, value) {
      localStorage.setItem("pixel-playground:" + key, value);
    },
    remove(key) {
      localStorage.removeItem("pixel-playground:" + key);
    },
  };
  const uuid = () => crypto.randomUUID();
  let entries = [],
    cart = 0,
    checkingOut = false,
    userId = "",
    registered = false,
    toastTimer;
  let localBackend = null,
    pendingOrder = null,
    serverBusy = false;
  const product = { content_id: "lamp-001", value: 99, currency: "CNY" };
  const providerNames = {
    lead: "Lead",
    meta: "Meta",
    google: "Google / dataLayer",
    tiktok: "TikTok",
    local: "本地",
  };
  const escape = (value) =>
    String(value).replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  function toast(message) {
    $("toast").textContent = message;
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      $("toast").hidden = true;
    }, 3500);
  }
  function renderLog() {
    $("event-count").textContent = entries.length;
    const filtered = entries.filter(
      (item) =>
        $("event-filter").value === "all" ||
        item.state === $("event-filter").value,
    );
    $("event-log").innerHTML = filtered.length
      ? filtered
          .map(
            (item) =>
              `<details class="log-item" data-log-id="${escape(item.id)}"${item.open ? " open" : ""}><summary><span class="log-main"><span class="log-name">${escape(item.name)}</span><span class="log-result" data-state="${escape(item.state)}">${escape(item.message)}</span></span><time class="log-time">${escape(item.time)}</time></summary><pre>${escape(JSON.stringify({ event_id: item.eventId, provider: item.provider, properties: item.properties, response: item.response }, null, 2))}</pre></details>`,
          )
          .join("")
      : '<div class="empty-log"><div class="empty-mark" aria-hidden="true">[ · ]</div><h3>等你的一次点击</h3><p>操作左侧场景，记录就会出现在这里。<br>点击记录可展开参数和回执。</p></div>';
  }
  $("event-log").addEventListener(
    "toggle",
    (event) => {
      const entry = entries.find(
        (item) => item.id === event.target.dataset.logId,
      );
      if (entry) entry.open = event.target.open;
    },
    true,
  );
  function log(
    name,
    properties = {},
    message = "等待调用",
    state = "pending",
    eventId = uuid(),
  ) {
    const item = {
      id: uuid(),
      name,
      properties,
      message,
      state,
      eventId,
      provider: providerNames[$("provider").value],
      time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
    };
    entries.unshift(item);
    entries = entries.slice(0, 100);
    renderLog();
    return item;
  }
  function update(item, state, message, response) {
    Object.assign(item, { state, message, response });
    renderLog();
    $("log-status").textContent = item.name + "：" + message;
  }
  function deadline(promise) {
    let timer;
    return Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(
          () =>
            reject(
              Error("15 秒内未收到回执，请到后台核对，原事件可能仍在处理。"),
            ),
          15000,
        );
      }),
    ]).finally(() => clearTimeout(timer));
  }
  function hasLead() {
    return typeof window.leadq === "function" && Boolean(window.leadq.version);
  }
  async function track(name, properties = {}, options = {}) {
    const item = log(
      name,
      properties,
      "正在调用…",
      "pending",
      options.event_id || uuid(),
    );
    window.dispatchEvent(
      new CustomEvent("pixel-playground:event", {
        detail: { name, properties, event_id: item.eventId },
      }),
    );
    try {
      switch ($("provider").value) {
        case "lead": {
          if (!hasLead())
            throw Error("Lead SDK 未就绪，请先安装像素代码并检查网站来源。");
          const response = await deadline(
            window.leadq("track", name, properties, {
              ...options,
              event_id: item.eventId,
            }),
          );
          update(
            item,
            "success",
            "接口已返回，请在后台核对事件与测试范围。",
            response,
          );
          break;
        }
        case "meta":
          if (typeof window.fbq !== "function")
            throw Error("未找到 fbq，请检查 Meta 安装代码。");
          window.fbq(
            ["ReportExported", "Activation"].includes(name)
              ? "trackCustom"
              : "track",
            name,
            properties,
            { eventID: item.eventId },
          );
          update(
            item,
            "called",
            "已调用 Meta SDK；浏览器调用不表示平台已接收。",
          );
          break;
        case "google": {
          if (!Array.isArray(window.dataLayer))
            throw Error("未找到 dataLayer，请先安装 Google 代码。");
          const names = {
            PageView: "page_view",
            ViewContent: "view_item",
            AddToCart: "add_to_cart",
            InitiateCheckout: "begin_checkout",
            Purchase: "purchase",
            CompleteRegistration: "sign_up",
            Lead: "generate_lead",
          };
          const data = { ...properties, event_id: item.eventId };
          if (
            [
              "ViewContent",
              "AddToCart",
              "InitiateCheckout",
              "Purchase",
            ].includes(name)
          )
            data.items = [
              {
                item_id: "lamp-001",
                item_name: "一盏小台灯",
                price: 99,
                quantity: properties.quantity || 1,
              },
            ];
          if (properties.order_id) data.transaction_id = properties.order_id;
          if (typeof window.gtag === "function")
            window.gtag("event", names[name] || name, data);
          else window.dataLayer.push({ event: names[name] || name, ...data });
          update(
            item,
            "called",
            "已调用 Google / dataLayer；请在平台调试工具中核对。",
          );
          break;
        }
        case "tiktok":
          if (typeof window.ttq?.track !== "function")
            throw Error("未找到 ttq.track，请检查 TikTok 安装代码。");
          window.ttq.track(
            name === "Purchase" ? "CompletePayment" : name,
            properties,
            { event_id: item.eventId },
          );
          update(
            item,
            "called",
            "已调用 TikTok SDK；浏览器调用不表示平台已接收。",
          );
          break;
        default:
          update(item, "local", "仅记录本地操作，未发送到像素服务。");
      }
    } catch (error) {
      update(item, "error", String(error.message || error));
    }
    return item;
  }
  window.pixelPlayground = { track };
  $("site-origin").textContent = location.origin;
  $("pixel-code").value = storage.get("code");
  $("provider").value = storage.get("provider", "lead");
  if (!providerNames[$("provider").value]) $("provider").value = "lead";
  $("user-id").value = "demo-" + uuid().slice(0, 8);
  const query = new URL(location.href).searchParams;
  $("test-code").value = query.get("lead_test") || "";
  $("ref-code").value = query.get("lr") || query.get("lead_ref") || "";
  $("copy-origin").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(location.origin);
      toast("网站地址已复制");
    } catch {
      toast("复制失败，请手动复制上方网站地址。");
    }
  });
  $("install-pixel").addEventListener("click", () => {
    const code = $("pixel-code").value.trim();
    if (!code) {
      $("install-feedback").textContent = "请先粘贴安装代码。";
      $("pixel-code").focus();
      return;
    }
    if (/^\s*</.test(code) && !/<script[\s>]/i.test(code)) {
      $("install-feedback").textContent =
        "请粘贴包含 <script> 的安装代码，或纯 JavaScript。";
      return;
    }
    try {
      storage.set("code", code);
      storage.set("provider", $("provider").value);
      location.reload();
    } catch {
      $("install-feedback").textContent =
        "浏览器禁止本地存储，未能保存代码。可直接编辑 index.html 安装。";
    }
  });
  $("remove-pixel").addEventListener("click", () => {
    try {
      storage.remove("code");
      location.reload();
    } catch {
      toast("未能清除本地存储。");
    }
  });
  $("provider").addEventListener("change", () => {
    try {
      storage.set("provider", $("provider").value);
    } catch {}
    refreshStatus();
  });
  function refreshStatus() {
    const provider = $("provider").value,
      boot = window.pixelBoot;
    const ready =
      provider === "lead"
        ? hasLead()
        : provider === "meta"
          ? typeof window.fbq === "function"
          : provider === "google"
            ? Array.isArray(window.dataLayer)
            : provider === "tiktok"
              ? typeof window.ttq?.track === "function"
              : false;
    $("sdk-status").textContent =
      provider === "local" ? "仅本地" : ready ? "SDK 可调用" : "未就绪";
    $("sdk-status").dataset.state = ready
      ? "success"
      : boot?.errors.length
        ? "error"
        : "idle";
    $("setup-summary").textContent = ready
      ? "SDK 可调用 · 可重新安装 ⌄"
      : boot?.state === "empty"
        ? "尚未安装 ⌄"
        : boot?.errors.length
          ? "请检查安装代码 ⌄"
          : "安装代码已执行 ⌄";
  }
  async function testApi(path, data) {
    const response = await fetch("/test-api/" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const result = await response.json();
    if (!response.ok)
      throw Error(
        typeof result.detail === "object"
          ? result.detail.message
          : result.detail || "测试服务请求失败",
      );
    return result;
  }
  function renderCart() {
    $("cart-count").textContent = cart;
    $("cart-description").textContent = cart
      ? `${cart} 件小台灯 · 合计 ¥ ${(cart * 99).toFixed(2)}`
      : "添加一件商品，开始结账测试。";
    $("checkout").disabled = !cart || serverBusy;
    $("purchase").disabled = !checkingOut || !cart || serverBusy;
    $("purchase").textContent = `浏览器模拟支付 ¥ ${(cart * 99).toFixed(2)}`;
    $("server-purchase").disabled = !pendingOrder || serverBusy;
  }
  function showTestState(state) {
    const showing = state.state !== "inactive";
    $("test-session").hidden = !showing;
    $("test-session-title").textContent =
      state.state === "active"
        ? "测试会话进行中"
        : state.state === "ended"
          ? "测试已结束，网站状态已清理"
          : "测试已过期或无效";
    $("test-session-description").textContent =
      state.state === "active"
        ? "按正常业务流程操作。浏览器事件与订单自动携带同一个测试上下文。"
        : state.state === "ended"
          ? "测试码、测试身份与测试会话已清理。已保存的测试订单仍保留原测试范围，不会转为正式支付。"
          : "已停止上报。重新打开有效测试链接，或点击结束测试后继续。";
    $("end-test").disabled = state.state === "ended";
    if (state.state !== "active") {
      $("test-code").value = "";
      userId = "";
      registered = false;
      $("identity-status").textContent = "测试身份已清理，SDK 已恢复原访客状态";
    }
  }
  window.addEventListener("lead:test-state", (event) =>
    showTestState(event.detail),
  );
  $("end-test").addEventListener("click", async () => {
    $("end-test").disabled = true;
    try {
      if (!hasLead()) throw Error("Lead SDK 未就绪。");
      await window.leadq("endTest");
      log("EndTest", {}, "测试会话已结束，SDK 状态已清理。", "local");
      toast("测试已结束，无需修改业务埋点代码。");
    } catch (error) {
      toast(error.message);
      $("end-test").disabled = false;
    }
  });
  $("load-local-pixel").addEventListener("click", () => {
    if (!localBackend?.snippet) return;
    $("pixel-code").value = localBackend.snippet;
    $("provider").value = "lead";
    $("install-feedback").textContent =
      "已填入本地验收连接，点击应用并刷新即可。";
  });
  fetch("/test-api/status")
    .then((response) => (response.ok ? response.json() : null))
    .then((value) => {
      if (value?.enabled) {
        localBackend = value;
        $("load-local-pixel").hidden = false;
      }
    })
    .catch(() => {});
  $("view-content").addEventListener("click", () => {
    track("ViewContent", { ...product });
    toast("一盏小台灯 · 珊瑚色 · 测试商品 ¥ 99.00");
  });
  $("add-cart").addEventListener("click", () => {
    cart++;
    checkingOut = false;
    pendingOrder = null;
    renderCart();
    track("AddToCart", { ...product, quantity: 1 });
    toast("小台灯已加入测试购物车");
  });
  $("checkout").addEventListener("click", async () => {
    checkingOut = true;
    pendingOrder = null;
    serverBusy = true;
    renderCart();
    await track("InitiateCheckout", {
      ...product,
      value: cart * 99,
      quantity: cart,
    });
    try {
      if (localBackend && hasLead() && $("provider").value === "lead") {
        const source = await window.leadq("attribution");
        pendingOrder = await testApi("orders", {
          quantity: cart,
          user_id: userId || null,
          lead_attribution: source,
        });
        log(
          "CreateOrder",
          { order_id: pendingOrder.order_id },
          "测试订单已保存完整归因快照，可切换页面后再模拟支付。",
          "local",
        );
        $("checkout-feedback").textContent =
          "测试订单已创建，点击服务端模拟支付。订单会自动沿用创建时的测试上下文。";
      } else
        $("checkout-feedback").textContent =
          "可测试浏览器模拟支付。服务端支付需要启用本地测试后端，并从完整流程测试链接进入。";
    } catch (error) {
      $("checkout-feedback").textContent = error.message;
      log("CreateOrder", {}, error.message, "error");
    } finally {
      serverBusy = false;
      renderCart();
    }
  });
  $("server-purchase").addEventListener("click", async () => {
    if (!pendingOrder) return;
    const savedOrder = pendingOrder,
      item = log(
        "Purchase",
        {
          order_id: savedOrder.order_id,
          value: savedOrder.value,
          currency: "CNY",
        },
        "网站服务端正在回传…",
        "pending",
        "purchase:" + savedOrder.order_id,
      );
    serverBusy = true;
    renderCart();
    try {
      const result = await testApi(
        "orders/" + savedOrder.order_id + "/pay",
        {},
      );
      update(
        item,
        "success",
        "网站服务端回传已接收，范围为测试。请在后台核对服务端回执。",
        result.receipt,
      );
      cart = 0;
      checkingOut = false;
      pendingOrder = null;
      $("checkout-feedback").textContent =
        "服务端模拟支付完成，未发生真实扣款。订单：" + savedOrder.order_id;
      toast("服务端 Purchase 测试回传已接收。");
    } catch (error) {
      update(item, "error", error.message);
      $("checkout-feedback").textContent = error.message;
    } finally {
      serverBusy = false;
      renderCart();
    }
  });
  $("purchase").addEventListener("click", () => {
    const order = "demo-order-" + uuid(),
      count = cart;
    track(
      "Purchase",
      { ...product, value: count * 99, quantity: count, order_id: order },
      { event_id: "purchase:" + order },
    );
    cart = 0;
    checkingOut = false;
    renderCart();
    $("checkout-feedback").textContent =
      "模拟支付完成。订单编号：" +
      order +
      "。此为浏览器事件，不能代替服务端支付确认。";
    toast("模拟订单已完成，查看右侧事件记录。");
  });
  $("register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submittedUser = $("user-id").value.trim();
    if (!submittedUser) {
      $("user-id").focus();
      return;
    }
    const button = event.submitter;
    button.disabled = true;
    try {
      if ($("provider").value === "lead") {
        if (!hasLead())
          throw Error("Lead SDK 未就绪，未能关联用户或发送注册事件。");
        const identity = log(
          "Identify",
          { user_id: submittedUser },
          "正在关联用户…",
        );
        try {
          const response = await deadline(
            window.leadq("identify", submittedUser),
          );
          update(identity, "success", "关联接口已返回。", response);
        } catch (error) {
          update(identity, "error", error.message);
          throw error;
        }
      }
      userId = submittedUser;
      registered = true;
      $("identity-status").textContent = "测试用户：" + userId;
      await track("CompleteRegistration", {}, { event_id: "signup:" + userId });
      toast("注册操作已触发，相同用户编号可用于测试去重。");
    } catch (error) {
      toast(error.message);
      log("CompleteRegistration", {}, error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
  $("reset-user").addEventListener("click", async () => {
    const item = log("Reset", {}, "正在退出测试身份…");
    try {
      if ($("provider").value === "lead") {
        if (!hasLead()) throw Error("Lead SDK 未就绪。");
        const response = await deadline(window.leadq("reset"));
        update(item, "success", "身份已重置，PageView 接口已返回。", response);
      } else
        update(
          item,
          "local",
          "页面测试身份已清除；其他平台身份请使用各平台的重置接口。",
        );
      userId = "";
      registered = false;
      $("identity-status").textContent = "当前为匿名访客";
      $("user-id").value = "demo-" + uuid().slice(0, 8);
    } catch (error) {
      update(item, "error", error.message);
    }
  });
  $("custom-form").addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const properties = JSON.parse($("event-properties").value);
      if (
        properties === null ||
        typeof properties !== "object" ||
        Array.isArray(properties)
      )
        throw Error('事件属性必须是 JSON 对象，例如 {"action":"测试"}。');
      const name = $("event-name").value.trim();
      if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(name))
        throw Error("事件名称需以字母开头，只包含字母、数字和下划线。");
      $("custom-feedback").textContent = "已触发，请查看右侧调用记录。";
      track(
        name,
        properties,
        $("event-id").value.trim()
          ? { event_id: $("event-id").value.trim() }
          : {},
      );
    } catch (error) {
      $("custom-feedback").textContent = error.message;
    }
  });
  $("query-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const url = new URL(location.href);
    for (const [key, value] of [
      ["lead_test", $("test-code").value.trim()],
      ["lr", $("ref-code").value.trim()],
    ])
      value ? url.searchParams.set(key, value) : url.searchParams.delete(key);
    url.searchParams.delete("lead_ref");
    location.assign(url.href);
  });
  $("page-view").addEventListener("click", () => track("PageView"));
  $("clear-log").addEventListener("click", () => {
    entries = [];
    renderLog();
    toast("本页记录已清空，后台事件不会被删除。");
  });
  $("event-filter").addEventListener("change", renderLog);
  $("export-log").addEventListener("click", () => {
    const blob = new Blob(
      [
        JSON.stringify(
          {
            exported_at: new Date().toISOString(),
            page_url: location.href,
            entries,
          },
          null,
          2,
        ),
      ],
      { type: "application/json" },
    );
    const href = URL.createObjectURL(blob),
      link = document.createElement("a");
    link.href = href;
    link.download = "pixel-events-" + Date.now() + ".json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
  });
  window.addEventListener("pixel:loader-error", (event) => {
    log("ScriptError", {}, event.detail, "error");
    refreshStatus();
  });
  renderLog();
  refreshStatus();
  for (const error of window.pixelBoot?.errors || [])
    log("ScriptError", {}, error, "error");
  (async () => {
    await window.pixelBoot?.promise;
    for (
      let attempt = 0;
      attempt < 60 &&
      storage.get("code") &&
      !hasLead() &&
      $("provider").value === "lead";
      attempt++
    )
      await new Promise((resolve) => setTimeout(resolve, 200));
    refreshStatus();
    if (hasLead()) {
      $("setup").open = false;
      if (typeof window.leadq === "function")
        window
          .leadq("testStatus")
          .then(showTestState)
          .catch(() => {});
      const item = log("SDK ready / 首次访问", {}, "等待首次访问回执…");
      try {
        const response = await deadline(
          window.leadq("ready", (attribution) => attribution),
        );
        update(
          item,
          "success",
          "SDK 初始请求链已完成，访问记录请到后台核对。",
          response,
        );
      } catch (error) {
        update(item, "error", error.message);
        $("setup").open = true;
      }
    } else if (storage.get("code") && $("provider").value === "lead")
      log(
        "SDK ready",
        {},
        "12 秒内未找到 Lead SDK。检查脚本地址、网络与安装代码。",
        "error",
      );
    if (!registered && hasLead())
      $("identity-status").textContent =
        "页面尚未关联用户；SDK 可能沿用当前会话身份，可点击退出重置。";
  })();
})();
