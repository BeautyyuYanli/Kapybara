<script setup lang="ts">
import { onScopeDispose, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  getSession,
  getModel,
  createSessionAndSchedule,
  updateSession,
  type CreateSessionAndSchedule,
  type UpdateSession,
} from "@/api/generated";
import { useResource } from "@/composables/useResource";
import { describeError } from "@/api/errors";
import { parseObject, optionalInteger } from "@/forms";
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
  compaction_threshold_tokens: "",
  compaction_replay_turns: "10",
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
watch(resource.data, (data) => {
  if (data)
    Object.assign(draft, data, {
      model_settings: JSON.stringify(data.model_settings, null, 2),
      compaction_threshold_tokens: data.compaction_threshold_tokens?.toString() ?? "",
      compaction_replay_turns: String(data.compaction_replay_turns),
    });
});
const model = useResource(
  () => [draft.provider_id, draft.model_name],
  async (signal) =>
    draft.provider_id && draft.model_name
      ? (
          await getModel({
            path: { provider_id: draft.provider_id, model_name: draft.model_name },
            signal,
            throwOnError: true,
          })
        ).data
      : null,
);
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
  let settings: CreateSessionAndSchedule["model_settings"],
    threshold: number | null,
    replay: number | null;
  try {
    settings = parseObject(draft.model_settings);
  } catch {
    fields.value.model_settings = "请输入 JSON 对象。";
    error.value = "请修正 JSON。";
    return;
  }
  try {
    threshold = optionalInteger(draft.compaction_threshold_tokens);
  } catch {
    fields.value.compaction_threshold_tokens = "请输入正整数。";
    return;
  }
  try {
    replay = optionalInteger(draft.compaction_replay_turns, 0);
    if (replay === null) throw new Error();
  } catch {
    fields.value.compaction_replay_turns = "请输入非负整数。";
    return;
  }
  saving.value = true;
  try {
    const body: UpdateSession & CreateSessionAndSchedule = {
      title: draft.title,
      provider_id: draft.provider_id,
      model_name: draft.model_name,
      model_settings: settings,
      compaction_threshold_tokens: threshold,
      compaction_replay_turns: replay,
    };
    if (id) {
      await updateSession({ path: { session_id: id }, body, throwOnError: true });
      if (submittedVersion !== viewVersion) return;
      message.value = "已保存，下次启动生效。";
      await resource.refresh();
    } else {
      // Configuration only: deliberately omit the optional input field.
      const { data } = await createSessionAndSchedule({ body, throwOnError: true });
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
      <span class="eyebrow">03 / SESSIONS</span>
      <h1>{{ id ? "编辑 Session" : "创建 Session" }}</h1>
      <p class="muted">选择模型，设定会话的上下文策略。</p>
    </div>
    <p class="muted">配置更新对下次启动生效。</p>
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
        <p v-if="model.error.value" class="error" role="alert">
          模型容量读取失败：{{ model.error.value }}
          <button type="button" @click="model.refresh">重试</button>
        </p>
        <p
          v-if="
            model.data.value &&
            model.data.value.context_window === null &&
            !draft.compaction_threshold_tokens
          "
          role="status"
        >
          {{
            id
              ? "模型容量未知，运行前需要设置摘要阈值。仍可保存配置。"
              : "模型容量未知，创建时将保存默认摘要阈值 183500。"
          }}
        </p>
        <JsonObjectField
          id="model_settings"
          v-model="draft.model_settings"
          label="模型设置 JSON"
          :error="fields.model_settings"
        />
        <FormField
          id="compaction_threshold_tokens"
          label="摘要阈值（可选）"
          :help="
            id
              ? '留空在下次启动时采用模型容量的 70%；容量未知时需设置阈值。'
              : '留空采用模型容量的 70%；创建时容量未知则保存默认阈值 183500。'
          "
          :error="fields.compaction_threshold_tokens"
          v-slot="f"
          ><input
            v-model="draft.compaction_threshold_tokens"
            :id="f.id"
            type="number"
            min="1"
            step="1"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <FormField
          id="compaction_replay_turns"
          label="回放轮数"
          help="默认 10，允许 0。"
          :error="fields.compaction_replay_turns"
          v-slot="f"
          ><input
            v-model="draft.compaction_replay_turns"
            :id="f.id"
            type="number"
            required
            min="0"
            step="1"
            :aria-describedby="f.describedby"
            :aria-invalid="f.invalid"
        /></FormField>
        <button class="primary" type="submit">{{ saving ? "保存中…" : "保存 Session" }}</button>
      </fieldset>
    </form>
  </section>
</template>
