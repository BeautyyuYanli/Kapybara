import { test, expect } from "@playwright/test";
import type { ProviderRecord, ModelRecord, SessionRecord } from "../src/api/generated";
const id = "00000000-0000-0000-0000-000000000001";
const provider: ProviderRecord = {
  id,
  name: "Demo Provider",
  provider_class: "pydantic_ai.providers.google:GoogleProvider",
  model_class: "pydantic_ai.models.google:GoogleModel",
  base_url: null,
  provider_kwargs: { retry_options: { attempts: 3 } },
  created_at: "",
  updated_at: "",
};
const model: ModelRecord = {
  provider_id: id,
  model_name: "vendor/model",
  name: "Slash model",
  context_window: null,
  settings: {},
  created_at: "",
  updated_at: "",
};
const session: SessionRecord = {
  id,
  title: "Demo Session",
  provider_id: id,
  model_name: model.model_name,
  model_settings: { temperature: 0.2 },
  compaction_threshold_tokens: null,
  compaction_replay_turns: 10,
  created_at: "",
  updated_at: "",
};

test.beforeEach(async ({ page }) => {
  await page.route("http://127.0.0.1:5173/api/**", async (route) => {
    const url = new URL(route.request().url()),
      path = decodeURIComponent(url.pathname);
    let data: unknown;
    if (path === "/api/providers") data = { items: [provider], has_more: false };
    else if (path === `/api/providers/${id}`) data = provider;
    else if (path === "/api/models") data = { items: [model], has_more: false };
    else if (path.startsWith(`/api/models/${id}/`)) data = model;
    else if (path === "/api/sessions")
      data = route.request().method() === "POST" ? session : { items: [session], has_more: false };
    else if (path === `/api/sessions/${id}`) data = session;
    else data = { detail: "Not found" };
    await route.fulfill({ json: data });
  });
});

test("mobile navigation covers the bottom edge as browser safe areas change", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 740 });
  const cdp = await page.context().newCDPSession(page);
  await page.goto("sessions/new");
  await expect(page.getByRole("button", { name: "保存 Session" })).toBeVisible();
  const navigation = page.getByRole("navigation", { name: "主导航" });

  // Emulate the actual CSS environment variables, including toolbar expansion
  // after the gesture area was exposed. Plain viewport resizing leaves these zero.
  for (const maximum of [24, 36]) {
    const heights: number[] = [];
    for (const bottom of [maximum, 0, maximum]) {
      await page.setViewportSize({ width: 390, height: 716 + bottom });
      await cdp.send("Emulation.setSafeAreaInsetsOverride", {
        insets: { bottom, bottomMax: maximum },
      });
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
      const viewportHeight = await page.evaluate(() => window.innerHeight);
      const bar = await page.locator(".site-header").boundingBox();
      const tab = await navigation.getByRole("link", { name: "Model", exact: true }).boundingBox();
      const save = await page.getByRole("button", { name: "保存 Session" }).boundingBox();
      expect(bar).not.toBeNull();
      expect(tab).not.toBeNull();
      expect(save).not.toBeNull();
      heights.push(bar!.height);
      // Each tab's background and divider reach the viewport edge without an
      // empty strip; the last form action remains above the navigation.
      expect(tab!.y + tab!.height).toBeGreaterThanOrEqual(viewportHeight);
      expect(save!.y + save!.height).toBeLessThanOrEqual(bar!.y);
      const labelBottom = await navigation
        .getByRole("link", { name: "Model", exact: true })
        .evaluate((link) => {
          const text = document.createRange();
          text.selectNodeContents(link);
          return text.getBoundingClientRect().bottom;
        });
      expect(labelBottom).toBeLessThanOrEqual(viewportHeight - bottom);
    }
    expect(new Set(heights).size).toBe(1);
  }
  await navigation.getByRole("link", { name: "Model", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Model", exact: true })).toBeVisible();
});

test("provider PATCH preserves blank key, replaces JSON, and clears URL", async ({ page }) => {
  await page.goto(`providers/${id}`);
  await expect(page.getByLabel("其他连接参数 JSON")).toHaveValue(
    JSON.stringify(provider.provider_kwargs, null, 2),
  );
  await page.getByLabel("其他连接参数 JSON").fill("{}");
  const request = page.waitForRequest((request) => request.method() === "PATCH");
  await page.getByRole("button", { name: "保存 Provider" }).click();
  expect((await request).postDataJSON()).toEqual({
    name: provider.name,
    base_url: null,
    provider_kwargs: {},
  });
  await expect(page.getByText("已保存。", { exact: true })).toBeVisible();
});

test("JSON arrays block submit; server field errors preserve draft", async ({ page }) => {
  await page.goto(`providers/${id}`);
  await page.getByLabel("其他连接参数 JSON").fill("[]");
  await page.getByRole("button", { name: "保存 Provider" }).click();
  await expect(page.getByText("请修正 JSON。")).toBeVisible();
  await page.getByLabel("其他连接参数 JSON").fill("{}");
  await page.route(`**/api/providers/${id}`, (route) =>
    route.request().method() === "PATCH"
      ? route.fulfill({
          status: 422,
          json: { detail: [{ loc: ["body", "name"], msg: "Invalid name", type: "value_error" }] },
        })
      : route.fallback(),
  );
  await page.getByLabel("名称", { exact: true }).fill("Draft");
  await page.getByRole("button", { name: "保存 Provider" }).click();
  await expect(page.getByText("Invalid name", { exact: true })).toBeVisible();
  await expect(page.getByLabel("名称", { exact: true })).toHaveValue("Draft");
});

test("model slash identity uses router encoding once and nullable context PATCH", async ({
  page,
}) => {
  await page.goto("models");
  await page.getByRole("link", { name: "Slash model" }).click();
  await expect(page.getByLabel("模型名称")).toHaveValue("vendor/model");
  const request = page.waitForRequest((request) => request.method() === "PATCH");
  await page.getByRole("button", { name: "保存 Model" }).click();
  const saved = await request;
  expect(decodeURIComponent(new URL(saved.url()).pathname)).toBe(`/api/models/${id}/vendor/model`);
  expect(saved.postDataJSON()).toEqual({ name: model.name, settings: {}, context_window: null });
});

test("session configuration create omits input and permits zero replay", async ({ page }) => {
  await page.goto("sessions/new");
  await page.getByLabel("Provider", { exact: true }).selectOption(id);
  await page.getByLabel("Model", { exact: true }).selectOption(model.model_name);
  await expect(
    page.getByText("模型容量未知，运行前需要设置摘要阈值。仍可保存配置。"),
  ).toBeVisible();
  await page.getByLabel("回放轮数", { exact: true }).fill("0");
  const request = page.waitForRequest((request) => request.method() === "POST");
  await page.getByRole("button", { name: "保存 Session" }).click();
  expect((await request).postDataJSON()).toEqual({
    title: "",
    provider_id: id,
    model_name: model.model_name,
    model_settings: {},
    compaction_threshold_tokens: null,
    compaction_replay_turns: 0,
  });
  await expect(page.getByRole("heading", { name: "编辑 Session" })).toBeVisible();
});

test("pagination uses returned count and filters reset offset", async ({ page }) => {
  await page.route("**/api/models?**", (route) =>
    route.fulfill({ json: { items: [model], has_more: true } }),
  );
  await page.goto("models?offset=25");
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page).toHaveURL(/offset=26/);
  await page.getByLabel("按 Provider 筛选").selectOption(id);
  await expect(page).not.toHaveURL(/offset=/);
});

test("delete conflict keeps dialog open; escape returns focus; mobile fits", async ({ page }) => {
  await page.goto("providers");
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.route(`**/api/providers/${id}`, (route) =>
    route.fulfill({ status: 409, json: { detail: "Resource conflict" } }),
  );
  await page.getByRole("button", { name: "确认删除" }).click();
  await expect(page.getByRole("alertdialog")).toContainText("仍被 Session 引用");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "删除", exact: true })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.screenshot({
    path: `test-results/providers-${test.info().project.name}.png`,
    fullPage: true,
  });
});

test("read errors are not empty lists, and unknown pages show a not-found heading", async ({
  page,
}) => {
  await page.route("**/api/providers?**", (route) =>
    route.fulfill({ status: 500, json: { detail: "hidden server internals" } }),
  );
  await page.goto("providers");
  await expect(page.getByRole("button", { name: "重试", exact: true })).toBeVisible();
  await expect(page.getByText("暂无 Provider 配置。")).toHaveCount(0);
  await expect(page.getByText("hidden server internals")).toHaveCount(0);
  await page.goto("unknown");
  await expect(page.getByRole("heading", { name: "页面不存在" })).toBeVisible();
});

test("paged options keep current selection and model switch preserves JSON draft", async ({
  page,
}) => {
  const secondId = "00000000-0000-0000-0000-000000000002";
  const secondModel: ModelRecord = { ...model, provider_id: secondId };
  let currentSession: SessionRecord = session;
  await page.route("**/api/models?**", (route) => {
    const providerId = new URL(route.request().url()).searchParams.get("provider_id");
    return route.fulfill({
      json: { items: [providerId === secondId ? secondModel : model], has_more: false },
    });
  });
  await page.route(`**/api/models/${secondId}/**`, (route) => route.fulfill({ json: secondModel }));
  await page.route(`**/api/sessions/${id}`, (route) => {
    if (route.request().method() === "PATCH") {
      currentSession = {
        ...session,
        provider_id: secondId,
        model_name: secondModel.model_name,
        model_settings: { temperature: 0.7 },
      };
    }
    return route.fulfill({ json: currentSession });
  });
  await page.route("**/api/providers?**", (route) => {
    const offset = new URL(route.request().url()).searchParams.get("offset");
    return route.fulfill({
      json: {
        items:
          offset === "0" ? [{ ...provider, id: secondId, name: "Other Provider" }] : [provider],
        has_more: offset === "0",
      },
    });
  });
  await page.goto(`sessions/${id}`);
  await expect(page.getByLabel("Provider", { exact: true })).toHaveValue(id);
  await page.getByRole("button", { name: "加载更多 Provider" }).click();
  await expect(page.getByRole("option", { name: "Demo Provider", exact: true })).toHaveCount(1);
  await page.getByLabel("模型设置 JSON").fill('{"temperature":0.7}');
  await page.getByLabel("Provider", { exact: true }).selectOption(secondId);
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue("");
  await expect(page.getByLabel("模型设置 JSON")).toHaveValue('{"temperature":0.7}');
  await page.getByLabel("Model", { exact: true }).selectOption(model.model_name);
  const request = page.waitForRequest((request) => request.method() === "PATCH");
  await page.getByRole("button", { name: "保存 Session" }).click();
  expect((await request).postDataJSON()).toMatchObject({
    provider_id: secondId,
    model_name: model.model_name,
    model_settings: { temperature: 0.7 },
  });
  await expect(page.getByText("已保存，下次启动生效。", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Provider", { exact: true })).toHaveValue(secondId);
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue(secondModel.model_name);
  await expect(page.getByLabel("模型设置 JSON")).toHaveValue(
    JSON.stringify(currentSession.model_settings, null, 2),
  );
});

test("discovery failure is retryable and success links to the provider directory", async ({
  page,
}) => {
  let attempts = 0;
  await page.route(`**/api/providers/${id}/discover-models`, (route) => {
    attempts++;
    return route.fulfill(
      attempts === 1 ? { status: 502, json: { detail: "failure" } } : { json: [model] },
    );
  });
  await page.goto("providers");
  await page.getByRole("button", { name: "发现模型", exact: true }).click();
  await expect(page.getByText("模型发现失败，请稍后重试。")).toBeVisible();
  await page.getByRole("button", { name: "发现模型", exact: true }).click();
  await page.getByRole("link", { name: "查看模型目录" }).click();
  await expect(page).toHaveURL(new RegExp(`provider_id=${id}`));
});

test("delete last item on later page returns one page", async ({ page }) => {
  let deleted = false;
  await page.route("**/api/providers?**", (route) =>
    route.fulfill({ json: { items: deleted ? [] : [provider], has_more: false } }),
  );
  await page.route(`**/api/providers/${id}`, (route) => {
    deleted = true;
    return route.fulfill({ status: 204 });
  });
  await page.goto("providers?offset=25");
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await page.getByRole("button", { name: "确认删除" }).click();
  await expect(page).not.toHaveURL(/offset=25/);
  await expect(page.getByText("暂无 Provider 配置。")).toBeVisible();
});

test("model selector loads beyond the first page", async ({ page }) => {
  await page.route("**/api/models?**", (route) => {
    const offset = new URL(route.request().url()).searchParams.get("offset");
    return route.fulfill({
      json: {
        items: offset === "0" ? [{ ...model, model_name: "first", name: "First model" }] : [model],
        has_more: offset === "0",
      },
    });
  });
  await page.goto("sessions/new");
  await page.getByLabel("Provider", { exact: true }).selectOption(id);
  await page.getByRole("button", { name: "加载更多 Model" }).click();
  await page.getByLabel("Model", { exact: true }).selectOption("vendor/model");
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue("vendor/model");
});

test("discovery cannot submit duplicates while pending", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let calls = 0;
  await page.route(`**/api/providers/${id}/discover-models`, async (route) => {
    calls++;
    await gate;
    await route.fulfill({ json: [model] });
  });
  await page.goto("providers");
  await page.getByRole("button", { name: "发现模型", exact: true }).click();
  await expect(page.getByRole("button", { name: "发现中…", exact: true })).toBeDisabled();
  release();
  await expect(page.getByRole("link", { name: "查看模型目录" })).toBeVisible();
  expect(calls).toBe(1);
});

// Branches below select a fixed resource/method case, never an observed test outcome.
/* eslint-disable playwright/no-conditional-in-test */
for (const kind of ["Provider", "Model", "Session"] as const) {
  for (const editing of [false, true]) {
    test(`late ${kind} ${editing ? "PATCH" : "POST"} cannot navigate or refresh a departed form`, async ({
      page,
    }) => {
      const collection = kind.toLowerCase() + "s";
      const path = editing
        ? kind === "Model"
          ? `models/edit?provider_id=${id}&model_name=vendor%2Fmodel`
          : `${collection}/${id}`
        : `${collection}/new`;
      const method = editing ? "PATCH" : "POST";
      let release!: () => void;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      let departed = false;
      const lateReads: string[] = [];
      page.on("request", (request) => {
        if (
          departed &&
          request.method() === "GET" &&
          new URL(request.url()).pathname.startsWith(`/api/${collection}/`)
        )
          lateReads.push(request.url());
      });
      await page.route(`http://127.0.0.1:5173/api/${collection}**`, async (route) => {
        if (route.request().method() !== method) return route.fallback();
        await gate;
        await route.fulfill({
          json: kind === "Provider" ? provider : kind === "Model" ? model : session,
        });
      });
      await page.goto(path);
      if (!editing) {
        if (kind === "Provider") {
          await page.getByLabel("名称", { exact: true }).fill("Pending provider");
          await page.getByLabel("API key", { exact: true }).fill("test-key");
        } else {
          await page.getByLabel("Provider", { exact: true }).selectOption(id);
          if (kind === "Model")
            await page.getByLabel("模型名称", { exact: true }).fill("vendor/model");
          else await page.getByLabel("Model", { exact: true }).selectOption(model.model_name);
        }
      }
      const submitted = page.waitForRequest((request) => request.method() === method);
      await page.getByRole("button", { name: `保存 ${kind}`, exact: true }).click();
      await submitted;
      // Navigate through the actual shell, then establish a different form's draft.
      const target = kind === "Session" ? "Provider" : "Session";
      await page
        .getByRole("navigation", { name: "主导航" })
        .getByRole("link", { name: target, exact: true })
        .click();
      await page.getByRole("link", { name: `创建 ${target}`, exact: true }).click();
      const draftField = page.getByLabel(target === "Provider" ? "名称" : "标题", { exact: true });
      await draftField.fill("Keep this new draft");
      const destination = page.url();
      departed = true;
      const completed = page.waitForResponse((response) => response.request().method() === method);
      release();
      await (await completed).finished();
      // Let the completed fetch and any router/UI continuation reach the browser's paint boundary.
      await page.evaluate(
        () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
      );
      await expect(page).toHaveURL(destination);
      await expect(draftField).toHaveValue("Keep this new draft");
      expect(lateReads).toEqual([]);
    });
  }
}

/* eslint-enable playwright/no-conditional-in-test */

for (const label of ["Provider 类引用", "Model 类引用"]) {
  test(`collapsed advanced required field ${label} opens and receives focus`, async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    await page.goto("providers/new");
    await page.getByLabel("名称", { exact: true }).fill("Provider");
    await page.getByLabel("API key", { exact: true }).fill("test-key");
    await page.getByText("高级连接配置", { exact: true }).click();
    const input = page.getByLabel(label, { exact: true });
    await input.fill("");
    await page.getByText("高级连接配置", { exact: true }).click();
    await expect(input).toBeHidden();
    await page.getByRole("button", { name: "保存 Provider", exact: true }).click();
    await expect(input).toBeVisible();
    await expect(input).toBeFocused();
    await expect(page.getByText("请填写类引用。", { exact: true })).toBeVisible();
    expect(consoleErrors.filter((message) => message.includes("not focusable"))).toEqual([]);
  });
}

// Fixed resource cases share the same pending/hash interaction.
/* eslint-disable playwright/no-conditional-in-test */
for (const kind of ["Provider", "Model", "Session"] as const) {
  test(`skip link preserves pending ${kind} creation without duplicate POST`, async ({ page }) => {
    const collection = kind.toLowerCase() + "s";
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let posts = 0;
    await page.route(`http://127.0.0.1:5173/api/${collection}`, async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      posts++;
      await gate;
      await route.fulfill({
        json: kind === "Provider" ? provider : kind === "Model" ? model : session,
      });
    });
    await page.goto(`${collection}/new`);
    if (kind === "Provider") {
      await page.getByLabel("名称", { exact: true }).fill("Pending provider");
      await page.getByLabel("API key", { exact: true }).fill("test-key");
    } else {
      await page.getByLabel("Provider", { exact: true }).selectOption(id);
      if (kind === "Model") await page.getByLabel("模型名称", { exact: true }).fill("vendor/model");
      else await page.getByLabel("Model", { exact: true }).selectOption(model.model_name);
    }
    const submitted = page.waitForRequest((request) => request.method() === "POST");
    await page.getByRole("button", { name: `保存 ${kind}`, exact: true }).click();
    await submitted;
    const skip = page.getByRole("link", { name: "跳至内容", exact: true });
    await skip.focus();
    await skip.click();
    await expect(page).toHaveURL(/#main$/);
    await expect(page.getByRole("button", { name: "保存中…", exact: true })).toBeDisabled();
    // The handler also rejects a second submission while the first write is pending.
    await page.locator("form").evaluate((form) => (form as HTMLFormElement).requestSubmit());
    release();
    await expect(page.getByRole("heading", { name: `编辑 ${kind}`, exact: true })).toBeVisible();
    expect(posts).toBe(1);
  });
}
/* eslint-enable playwright/no-conditional-in-test */

test("skip link keeps Model edit draft without reloading the same resource", async ({ page }) => {
  let reads = 0;
  await page.route(`http://127.0.0.1:5173/api/models/${id}/**`, async (route) => {
    reads++;
    await route.fulfill({ json: model });
  });
  await page.goto(`models/edit?provider_id=${id}&model_name=vendor%2Fmodel`);
  const name = page.getByLabel("显示名称", { exact: true });
  await expect(name).toHaveValue(model.name);
  await name.fill("Unsaved model name");
  const skip = page.getByRole("link", { name: "跳至内容", exact: true });
  await skip.focus();
  await skip.click();
  await expect(page).toHaveURL(/#main$/);
  await page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
  );
  await expect(name).toHaveValue("Unsaved model name");
  expect(reads).toBe(1);
});

for (const key of ["provider_id", "model_name"] as const) {
  const next = {
    ...model,
    [key]: key === "provider_id" ? "00000000-0000-0000-0000-000000000002" : "vendor/next",
    name: "Next model",
  };

  test(`Model ${key} changes still invalidate old writes while UI query preserves draft`, async ({
    page,
  }) => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let reads = 0;
    await page.route("http://127.0.0.1:5173/api/models/**", async (route) => {
      if (route.request().method() === "PATCH") {
        await gate;
        return route.fulfill({ json: model });
      }
      reads++;
      const original =
        decodeURIComponent(new URL(route.request().url()).pathname) ===
        `/api/models/${id}/${model.model_name}`;
      await route.fulfill({ json: original ? model : next });
    });
    await page.goto(`models/edit?provider_id=${id}&model_name=vendor%2Fmodel`);
    const name = page.getByLabel("显示名称", { exact: true });
    await expect(name).toHaveValue(model.name);
    await name.fill("Original draft");
    // Exercise same-document history navigation, retaining the mounted edit component.
    await page.evaluate(() => {
      const url = new URL(location.href);
      url.searchParams.set("panel", "notes");
      history.pushState(history.state, "", url);
      dispatchEvent(new PopStateEvent("popstate", { state: history.state }));
    });
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
    await expect(name).toHaveValue("Original draft");
    expect(reads).toBe(1);
    const submitted = page.waitForRequest((request) => request.method() === "PATCH");
    await page.getByRole("button", { name: "保存 Model", exact: true }).click();
    await submitted;
    await page.evaluate(
      ({ key, value }) => {
        const url = new URL(location.href);
        url.searchParams.set(key, value);
        history.pushState(history.state, "", url);
        dispatchEvent(new PopStateEvent("popstate", { state: history.state }));
      },
      { key, value: next[key] },
    );
    await expect(name).toHaveValue(next.name);
    await name.fill("New resource draft");
    const completed = page.waitForResponse((response) => response.request().method() === "PATCH");
    release();
    await (await completed).finished();
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
    await expect(name).toHaveValue("New resource draft");
    expect(reads).toBe(2);
  });
}
