<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { listSessions } from "@/api/generated";
import { useResource } from "@/composables/useResource";
import PageNavigation from "@/components/PageNavigation.vue";
import ProviderSelect from "@/components/ProviderSelect.vue";
import ModelSelect from "@/components/ModelSelect.vue";
const route = useRoute(),
  router = useRouter();
const offset = computed(() => Math.max(0, Number(route.query.offset) || 0));
const provider = computed({
  get: () => String(route.query.provider_id ?? ""),
  set: (value) => {
    void router.replace({ query: { provider_id: value || undefined } });
  },
});
const model = computed({
  get: () => String(route.query.model_name ?? ""),
  set: (value) => {
    void router.replace({
      query: { provider_id: provider.value || undefined, model_name: value || undefined },
    });
  },
});
const { data, pending, error, refresh } = useResource(
  () => route.fullPath,
  async (signal) =>
    (
      await listSessions({
        query: {
          provider_id: provider.value || undefined,
          model_name: model.value || undefined,
          offset: offset.value,
          limit: 25,
        },
        signal,
        throwOnError: true,
      })
    ).data,
);
function page(value: number) {
  return router.replace({ query: { ...route.query, offset: value || undefined } });
}
</script>
<template>
  <div class="page-heading">
    <div>
      <span class="eyebrow">03 / SESSIONS</span>
      <h1>Session</h1>
      <p class="muted">配置会话使用的模型与摘要策略</p>
    </div>
    <RouterLink class="button primary" to="/sessions/new">
      <span aria-hidden="true">＋</span>创建 Session
    </RouterLink>
  </div>
  <div class="filters">
    <div class="field">
      <label for="provider-filter">按 Provider 筛选</label
      ><ProviderSelect id="provider-filter" v-model="provider" />
    </div>
    <div class="field">
      <label for="model-filter">按 Model 筛选</label
      ><ModelSelect id="model-filter" v-model="model" :provider-id="provider" />
    </div>
  </div>
  <p v-if="pending" role="status">加载中…</p>
  <div v-else-if="error" role="alert">
    <p class="error">{{ error }}</p>
    <button @click="refresh">重试</button>
  </div>
  <template v-else-if="data">
    <div class="collection-heading">
      <span>会话配置</span><span>本页 {{ String(data.items.length).padStart(2, "0") }} 项</span>
    </div>
    <p v-if="!data.items.length" class="panel">暂无匹配的 Session 配置。</p>
    <ul v-else class="records">
      <li v-for="(item, index) in data.items" :key="item.id" class="record">
        <span class="record-index" aria-hidden="true">
          {{ String(offset + index + 1).padStart(2, "0") }}
        </span>
        <div>
          <h2>
            <RouterLink :to="`/sessions/${item.id}`">{{
              item.title || "未命名 Session"
            }}</RouterLink>
          </h2>
          <p>{{ item.model_name }}</p>
          <small>{{ item.id }}</small>
        </div>
        <p class="muted record-note">
          阈值 {{ item.compaction_threshold_tokens ?? "自动" }} · 回放
          {{ item.compaction_replay_turns }} 轮
        </p>
      </li>
    </ul>
    <PageNavigation
      :offset="offset"
      :count="data.items.length"
      :has-more="data.has_more"
      @change="page"
    />
  </template>
</template>
