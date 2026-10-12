<script setup lang="ts">
import { computed, onScopeDispose, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  getSession,
  createSession,
  updateSession,
  type CreateSession,
} from "@/api/generated";
import { useResource } from "@/composables/useResource";
import { describeError } from "@/api/errors";
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
const resource = useResource(
  () => id,
  async (signal) =>
    id ? (await getSession({ path: { session_id: id }, signal, throwOnError: true })).data : null,
);
const executionEditable = computed(() => !id || resource.data.value?.status === "ready");
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
async function save() {
  if (saving.value) return;
  const submittedVersion = viewVersion;
  error.value = "";
  message.value = "";
  fields.value = {};
  let body: CreateSession | undefined;
  if (executionEditable.value) {
    let settings: CreateSession["model_settings"];
    try {
      settings = parseObject(draft.model_settings);
    } catch {
      fields.value.model_settings = "请输入 JSON 对象。";
      error.value = "请修正 JSON。";
      return;
    }
    body = {
      title: draft.title,
      provider_id: draft.provider_id,
      model_name: draft.model_name,
      model_settings: settings,
    };
  }
  saving.value = true;
  try {
    if (id) {
      await updateSession({
        path: { session_id: id },
        body: body ?? { title: draft.title },
        throwOnError: true,
      });
      if (submittedVersion !== viewVersion) return;
      message.value = body ? "已保存，下次启动生效。" : "已保存。";
      await resource.refresh();
    } else if (body) {
      const { data } = await createSession({ body, throwOnError: true });
      if (submittedVersion !== viewVersion) return;
      await router.replace(`/sessions/${data.id}`);
    }
  } catch (e) {
    if (submittedVersion !== viewVersion) return;
    const failure = describeError(e, Object.keys(draft));
    error.value = failure.message;
    fields.value = failure.fields;
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
      <fieldset :disabled="saving">
        <FormField id="title" label="标题" :error="fields.title" v-slot="f"
          ><input
            v-model="draft.title"
            :id="f.id"
            maxlength="256"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <fieldset :disabled="!executionEditable">
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
        </fieldset>
        <small v-if="id">
          {{ executionEditable ? "配置更新对下次启动生效。" : "当前会话只能修改标题。" }}
        </small>
        <button class="primary" type="submit">{{ saving ? "保存中…" : "保存 Session" }}</button>
      </fieldset>
    </form>
  </section>
</template>
