<script setup lang="ts">
import { computed, onScopeDispose, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  getModel,
  createModel,
  updateModel,
  type CreateModel,
  type UpdateModel,
} from "@/api/generated";
import { useResource } from "@/composables/useResource";
import { describeError } from "@/api/errors";
import { parseObject, optionalInteger } from "@/forms";
import FormField from "@/components/FormField.vue";
import JsonObjectField from "@/components/JsonObjectField.vue";
import ProviderSelect from "@/components/ProviderSelect.vue";
const route = useRoute(),
  router = useRouter();
const editing = computed(() => route.path === "/models/edit");
// Only the edit model's composite key changes resource identity; hash and UI query do not.
const resourceIdentity = computed(() =>
  JSON.stringify([
    route.path,
    editing.value ? String(route.query.provider_id ?? "") : "",
    editing.value ? String(route.query.model_name ?? "") : "",
  ]),
);
// Writes may finish after navigation; only this view generation owns UI follow-up.
let viewVersion = 0;
watch(
  resourceIdentity,
  () => {
    viewVersion++;
    saving.value = false;
  },
  { flush: "sync" },
);
onScopeDispose(() => {
  viewVersion++;
});
const draft = reactive({
  provider_id: String(route.query.provider_id ?? ""),
  model_name: "",
  name: "",
  context_window: "",
  settings: "{}",
});
const saving = ref(false),
  message = ref(""),
  error = ref(""),
  fields = ref<Record<string, string>>({});
const resource = useResource(resourceIdentity, async (signal) =>
  editing.value
    ? (
        await getModel({
          path: {
            provider_id: String(route.query.provider_id ?? ""),
            model_name: String(route.query.model_name ?? ""),
          },
          signal,
          throwOnError: true,
        })
      ).data
    : null,
);
watch(resource.data, (data) => {
  if (data)
    Object.assign(draft, data, {
      context_window: data.context_window?.toString() ?? "",
      settings: JSON.stringify(data.settings, null, 2),
    });
});
async function save() {
  if (saving.value) return;
  const submittedVersion = viewVersion;
  error.value = "";
  message.value = "";
  fields.value = {};
  let settings: CreateModel["settings"], context: number | null;
  try {
    settings = parseObject(draft.settings);
  } catch {
    fields.value.settings = "请输入 JSON 对象。";
    error.value = "请修正 JSON。";
    return;
  }
  try {
    context = optionalInteger(draft.context_window);
  } catch {
    fields.value.context_window = "请输入正整数。";
    return;
  }
  saving.value = true;
  try {
    if (editing.value) {
      const body: UpdateModel = { name: draft.name, context_window: context, settings };
      await updateModel({
        path: { provider_id: draft.provider_id, model_name: draft.model_name },
        body,
        throwOnError: true,
      });
      if (submittedVersion !== viewVersion) return;
      message.value = "已保存。";
      await resource.refresh();
    } else {
      const body: CreateModel = {
        provider_id: draft.provider_id,
        model_name: draft.model_name,
        settings,
        ...(draft.name.trim() ? { name: draft.name } : {}),
        ...(context === null ? {} : { context_window: context }),
      };
      const { data } = await createModel({ body, throwOnError: true });
      if (submittedVersion !== viewVersion) return;
      await router.replace({
        path: "/models/edit",
        query: { provider_id: data.provider_id, model_name: data.model_name },
      });
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
    <div class="page-heading">
      <h1>{{ editing ? "编辑 Model" : "创建 Model" }}</h1>
      <RouterLink to="/models">返回列表</RouterLink>
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
        <FormField id="provider_id" label="Provider" :error="fields.provider_id" v-slot="f"
          ><input v-if="editing" :id="f.id" :value="draft.provider_id" readonly /><ProviderSelect
            v-else
            v-model="draft.provider_id"
            :id="f.id"
            required
            :describedby="f.describedby"
            :invalid="f.invalid"
        /></FormField>
        <FormField id="model_name" label="模型名称" :error="fields.model_name" v-slot="f"
          ><input
            v-model="draft.model_name"
            :id="f.id"
            required
            maxlength="256"
            :readonly="editing"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <FormField
          id="name"
          :label="editing ? '显示名称' : '显示名称（可选）'"
          :error="fields.name"
          v-slot="f"
          ><input
            v-model="draft.name"
            :id="f.id"
            :required="editing"
            maxlength="256"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <FormField
          id="context_window"
          label="Context window（可选）"
          :help="
            editing
              ? '清空将移除现有容量，显示为未知。'
              : '留空由服务端尝试推断，无法推断时保持未知。'
          "
          :error="fields.context_window"
          v-slot="f"
          ><input
            v-model="draft.context_window"
            :id="f.id"
            type="number"
            min="1"
            step="1"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <JsonObjectField
          id="settings"
          v-model="draft.settings"
          label="请求预设 JSON"
          :error="fields.settings"
        />
        <button class="primary" type="submit">{{ saving ? "保存中…" : "保存 Model" }}</button>
      </fieldset>
    </form>
  </section>
</template>
