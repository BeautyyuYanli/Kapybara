<script setup lang="ts">
import { computed, onMounted, onScopeDispose, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  getSession,
  getSessionActivity,
  createSession,
  updateSession,
  type CreateSessionRequest,
} from "@/api/generated";
import { useResource } from "@/composables/useResource";
import { ApiError, describeError, type ExecutionReference } from "@/api/errors";
import { newActivityId } from "@/api/activityId";
import { parseObject } from "@/forms";
import FormField from "@/components/FormField.vue";
import JsonObjectField from "@/components/JsonObjectField.vue";
import ProviderSelect from "@/components/ProviderSelect.vue";
import ModelSelect from "@/components/ModelSelect.vue";
const route = useRoute(),
  router = useRouter();
// The path identifies this page/resource; hash and unrelated query changes stay in the form.
// Writes may finish after navigation; only this view generation owns UI follow-up.
let viewVersion = 0;
watch(
  () => route.path,
  () => {
    viewVersion++;
    saving.value = false;
  },
  { flush: "sync" },
);
onScopeDispose(() => {
  viewVersion++;
});
const id = String(route.params.id ?? "");
const draft = reactive({
  title: "",
  provider_id: "",
  model_name: "",
  model_settings: "{}",
});
const saving = ref(false),
  message = ref(""),
  error = ref(""),
  fields = ref<Record<string, string>>({});
const storageKey = "kapy:session-create";
type PendingCreate = { intent: string; activity_id: string; run_id?: string };
const pendingCreate = ref<PendingCreate | null>(null);
if (!id) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
    if (saved && typeof saved.intent === "string" && typeof saved.activity_id === "string") {
      const intent = JSON.parse(saved.intent);
      Object.assign(draft, intent, { model_settings: JSON.stringify(intent.model_settings ?? {}, null, 2) });
      pendingCreate.value = saved;
    }
  } catch {
    sessionStorage.removeItem(storageKey);
  }
}
const resource = useResource(
  () => id,
  async (signal) =>
    id ? (await getSession({ path: { session_id: id }, signal, throwOnError: true })).data : null,
);
const executionEditable = computed(() =>
  id
    ? resource.data.value?.status === "ready" &&
      resource.data.value.current_activity_id === null &&
      resource.data.value.current_activity_run_id === null
    : !pendingCreate.value,
);
watch(resource.data, (data) => {
  if (data)
    Object.assign(draft, data, {
      model_settings: JSON.stringify(data.model_settings, null, 2),
    });
});
function changeProvider(value: string) {
  draft.provider_id = value;
  draft.model_name = "";
}
function abandonCreation() {
  sessionStorage.removeItem(storageKey);
  pendingCreate.value = null;
  error.value = "";
  message.value = "已放弃原创建引用；可以开始新的创建。";
}
function storedCreation(pending: PendingCreate): PendingCreate | undefined {
  try {
    const current = JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as PendingCreate | null;
    if (current?.activity_id === pending.activity_id && current.intent === pending.intent) return current;
  } catch {
    // An invalid or discarded intent cannot be restored by a late response.
  }
}
function retainCreationReference(pending: PendingCreate, reference: ExecutionReference) {
  if (reference.session_id !== pending.activity_id || reference.activity_id !== pending.activity_id) return;
  const current = storedCreation(pending);
  if (!current || (current.run_id && current.run_id !== reference.run_id)) return;
  current.run_id = reference.run_id;
  sessionStorage.setItem(storageKey, JSON.stringify(current));
  return current;
}
async function recoverCreation() {
  const pending = pendingCreate.value;
  if (!pending || saving.value) return;
  // A departed form's response may have pinned this request since we mounted.
  const current = storedCreation(pending);
  if (current) pending.run_id = current.run_id;
  const version = viewVersion;
  saving.value = true;
  error.value = "";
  try {
    // The Activity UUID is also the default business identity. A committed
    // resource remains useful even when its original native result was lost.
    const session = await getSession({ path: { session_id: pending.activity_id } });
    if (version !== viewVersion) return;
    if (session.data) {
      sessionStorage.removeItem(storageKey);
      await router.replace(`/sessions/${session.data.id}`);
      return;
    }
    const execution = await getSessionActivity({
      path: { session_id: pending.activity_id, activity_id: pending.activity_id },
      query: { run_id: pending.run_id },
    });
    if (version !== viewVersion) return;
    const retained = execution.data ? retainCreationReference(pending, execution.data) : undefined;
    if (execution.data && retained) {
      pendingCreate.value = retained;
      message.value = `创建状态：${execution.data.status}${execution.data.run_state ? ` / ${execution.data.run_state}` : ""}${execution.data.error ? `（${execution.data.error}）` : ""}。保留原创建引用，可再次查询。`;
    } else {
      error.value = "创建结果无法确认。已保留原引用；请再次查询，或显式放弃后重新创建。";
    }
  } catch {
    if (version === viewVersion)
      error.value = "创建结果无法确认。已保留原引用；请再次查询，或显式放弃后重新创建。";
  } finally {
    if (version === viewVersion) saving.value = false;
  }
}
onMounted(() => { if (pendingCreate.value) void recoverCreation(); });
async function save() {
  if (saving.value || !executionEditable.value) return;
  const submittedVersion = viewVersion;
  error.value = "";
  message.value = "";
  fields.value = {};
  let settings: CreateSessionRequest["model_settings"];
  try {
    settings = parseObject(draft.model_settings);
  } catch {
    fields.value.model_settings = "请输入 JSON 对象。";
    error.value = "请修正 JSON。";
    return;
  }
  const body: Omit<CreateSessionRequest, "activity_id"> = {
    title: draft.title,
    provider_id: draft.provider_id,
    model_name: draft.model_name,
    model_settings: settings,
  };
  saving.value = true;
  let submittedCreate: PendingCreate | undefined;
  try {
    if (id) {
      await updateSession({
        path: { session_id: id },
        body,
        throwOnError: true,
      });
      if (submittedVersion !== viewVersion) return;
      message.value = "已保存，下次启动生效。";
      await resource.refresh();
    } else {
      const pending = { intent: JSON.stringify(body), activity_id: newActivityId() };
      submittedCreate = pending;
      pendingCreate.value = pending;
      sessionStorage.setItem(storageKey, JSON.stringify(pending));
      const { data } = await createSession({
        body: { ...body, activity_id: pending.activity_id },
        throwOnError: true,
      });
      const current = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
      if (current?.activity_id === pending.activity_id) sessionStorage.removeItem(storageKey);
      if (submittedVersion !== viewVersion) return;
      await router.replace(`/sessions/${data.id}`);
    }
  } catch (e) {
    // A late response still pins its execution, even after this view departs.
    // Only the original intent still present in storage can receive that run ID.
    const reference = e instanceof ApiError ? e.reference : undefined;
    const retained = submittedCreate && reference
      ? retainCreationReference(submittedCreate, reference)
      : undefined;
    if (submittedVersion !== viewVersion) return;
    const failure = describeError(e, Object.keys(draft));
    error.value = failure.message;
    fields.value = failure.fields;
    if (!id && pendingCreate.value) {
      if (retained) pendingCreate.value = retained;
      saving.value = false;
      await recoverCreation();
    }
  } finally {
    if (submittedVersion === viewVersion) saving.value = false;
  }
}
</script>
<template>
  <section class="form-page">
    <RouterLink class="back-link" to="/sessions"><span aria-hidden="true">← </span>返回列表</RouterLink>
    <div class="page-heading">
      <h1>{{ id ? "编辑 Session" : "创建 Session" }}</h1>
    </div>
    <p v-if="resource.pending.value" role="status">加载中…</p>
    <div v-else-if="resource.error.value" role="alert">
      <p class="error">{{ resource.error.value }}</p>
      <button @click="resource.refresh">重试</button>
    </div>
    <form v-else @submit.prevent="save">
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="message" role="status">{{ message }}</p>
      <div v-if="pendingCreate" class="pending-create">
        <p>创建引用：{{ pendingCreate.activity_id }}</p>
        <button type="button" :disabled="saving" @click="recoverCreation">查询创建结果</button>
        <button type="button" :disabled="saving" @click="abandonCreation">放弃这次创建</button>
      </div>
      <fieldset :disabled="saving || !executionEditable">
        <FormField id="title" label="标题" :error="fields.title" v-slot="f"
          ><input
            v-model="draft.title"
            :id="f.id"
            maxlength="256"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <FormField id="provider_id" label="Provider" :error="fields.provider_id" v-slot="f"
          ><ProviderSelect
            :model-value="draft.provider_id"
            @update:model-value="changeProvider"
            :id="f.id"
            required
            :describedby="f.describedby"
            :invalid="f.invalid"
        /></FormField>
        <FormField id="model_name" label="Model" :error="fields.model_name" v-slot="f"
          ><ModelSelect
            v-model="draft.model_name"
            :provider-id="draft.provider_id"
            :id="f.id"
            required
            :describedby="f.describedby"
            :invalid="f.invalid"
        /></FormField>
        <JsonObjectField
          id="model_settings"
          v-model="draft.model_settings"
          label="模型设置 JSON"
          :error="fields.model_settings"
        />
        <small v-if="id">
          {{ executionEditable ? "配置更新对下次启动生效。" : "当前会话暂时不能修改。" }}
        </small>
        <button class="primary" type="submit">{{ saving ? "保存中…" : "保存 Session" }}</button>
      </fieldset>
    </form>
  </section>
</template>
