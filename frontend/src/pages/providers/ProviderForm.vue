<script setup lang="ts">
import { onScopeDispose, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  getProvider,
  createProvider,
  updateProvider,
  type CreateProviderWritable,
  type UpdateProviderWritable,
} from "@/api/generated";
import { useResource } from "@/composables/useResource";
import { describeError } from "@/api/errors";
import { parseObject } from "@/forms";
import { presets } from "./presets";
import FormField from "@/components/FormField.vue";
import JsonObjectField from "@/components/JsonObjectField.vue";
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
  name: "",
  api_key: "",
  base_url: "",
  provider_class: presets[0].provider as string,
  model_class: presets[0].model as string,
  provider_kwargs: "{}",
});
const preset = ref(0),
  saving = ref(false),
  message = ref(""),
  error = ref(""),
  fields = ref<Record<string, string>>({});
const resource = useResource(
  () => id,
  async (signal) =>
    id ? (await getProvider({ path: { provider_id: id }, signal, throwOnError: true })).data : null,
);
watch(resource.data, (data) => {
  if (data)
    Object.assign(draft, data, {
      api_key: "",
      base_url: data.base_url ?? "",
      provider_kwargs: JSON.stringify(data.provider_kwargs, null, 2),
    });
});
function applyPreset() {
  const item = presets[preset.value];
  if (item) {
    draft.provider_class = item.provider;
    draft.model_class = item.model;
  }
}
function revealInvalidClass(event: Event) {
  const input = event.target;
  if (!(input instanceof HTMLInputElement)) return;
  // Open synchronously so native constraint validation can focus the hidden input.
  (event.currentTarget as HTMLDetailsElement).open = true;
  fields.value[input.id] = "请填写类引用。";
}
async function save() {
  if (saving.value) return;
  const submittedVersion = viewVersion;
  error.value = "";
  message.value = "";
  fields.value = {};
  let kwargs: CreateProviderWritable["provider_kwargs"];
  try {
    kwargs = parseObject(draft.provider_kwargs);
  } catch {
    fields.value.provider_kwargs = "请输入 JSON 对象。";
    error.value = "请修正 JSON。";
    return;
  }
  saving.value = true;
  try {
    const common: UpdateProviderWritable = {
      name: draft.name,
      base_url: draft.base_url.trim() || null,
      provider_kwargs: kwargs,
      ...(draft.api_key ? { api_key: draft.api_key } : {}),
    };
    if (id) {
      await updateProvider({ path: { provider_id: id }, body: common, throwOnError: true });
      if (submittedVersion !== viewVersion) return;
      draft.api_key = "";
      message.value = "已保存。";
      await resource.refresh();
    } else {
      const body: CreateProviderWritable = {
        name: draft.name,
        api_key: draft.api_key,
        base_url: draft.base_url.trim() || null,
        provider_kwargs: kwargs,
        provider_class: draft.provider_class,
        model_class: draft.model_class,
      };
      const { data } = await createProvider({ body, throwOnError: true });
      if (submittedVersion !== viewVersion) return;
      draft.api_key = "";
      await router.replace(`/providers/${data.id}`);
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
    <RouterLink class="back-link" to="/providers"><span aria-hidden="true">← </span>返回列表</RouterLink>
    <div class="page-heading">
      <h1>{{ id ? "编辑 Provider" : "创建 Provider" }}</h1>
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
        <FormField id="name" label="名称" :error="fields.name" v-slot="f"
          ><input
            v-model="draft.name"
            :id="f.id"
            required
            maxlength="256"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <FormField v-if="!id" id="preset" label="协议预设" v-slot="f"
          ><select v-model="preset" :id="f.id" @change="applyPreset">
            <option v-for="(item, i) in presets" :key="item.label" :value="i">
              {{ item.label }}
            </option>
          </select></FormField
        >
        <FormField
          id="api_key"
          label="API key"
          :help="id ? '留空保持原有密钥。' : undefined"
          :error="fields.api_key"
          v-slot="f"
          ><input
            v-model="draft.api_key"
            :id="f.id"
            type="password"
            :required="!id"
            autocomplete="new-password"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <FormField
          id="base_url"
          label="Base URL（可选）"
          :help="id ? '清空恢复默认服务地址。' : '留空使用默认服务地址。'"
          :error="fields.base_url"
          v-slot="f"
          ><input
            v-model="draft.base_url"
            :id="f.id"
            type="url"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <details :open="!!id" @invalid.capture="revealInvalidClass">
          <summary>高级连接配置</summary>
          <div class="stack">
            <FormField
              id="provider_class"
              label="Provider 类引用"
              :error="fields.provider_class"
              v-slot="f"
              ><input
                v-model="draft.provider_class"
                :id="f.id"
                required
                :readonly="!!id"
                :aria-describedby="f.describedby"
                :aria-invalid="f.invalid"
            /></FormField>
            <FormField id="model_class" label="Model 类引用" :error="fields.model_class" v-slot="f"
              ><input
                v-model="draft.model_class"
                :id="f.id"
                required
                :readonly="!!id"
                :aria-describedby="f.describedby"
                :aria-invalid="f.invalid"
            /></FormField>
            <JsonObjectField
              id="provider_kwargs"
              v-model="draft.provider_kwargs"
              label="其他连接参数 JSON"
              :error="fields.provider_kwargs"
            />
          </div>
        </details>
        <button class="primary" type="submit">{{ saving ? "保存中…" : "保存 Provider" }}</button>
      </fieldset>
    </form>
  </section>
</template>
