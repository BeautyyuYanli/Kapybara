<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { listModels, deleteModel, type ModelRecord } from "@/api/generated";
import { useResource } from "@/composables/useResource";
import { describeError } from "@/api/errors";
import PageNavigation from "@/components/PageNavigation.vue";
import ConfirmDelete from "@/components/ConfirmDelete.vue";
import ProviderSelect from "@/components/ProviderSelect.vue";
const route = useRoute(),
  router = useRouter();
const offset = computed(() => Math.max(0, Number(route.query.offset) || 0));
const provider = computed({
  get: () => String(route.query.provider_id ?? ""),
  set: (value) => {
    void router.replace({ query: { provider_id: value || undefined } });
  },
});
const { data, pending, error, refresh } = useResource(
  () => route.fullPath,
  async (signal) =>
    (
      await listModels({
        query: { provider_id: provider.value || undefined, offset: offset.value, limit: 25 },
        signal,
        throwOnError: true,
      })
    ).data,
);
const actionError = ref("");
function page(value: number) {
  return router.replace({ query: { ...route.query, offset: value || undefined } });
}
async function remove(item: ModelRecord) {
  actionError.value = "";
  try {
    await deleteModel({
      path: { provider_id: item.provider_id, model_name: item.model_name },
      throwOnError: true,
    });
    await refresh();
    if (data.value?.items.length === 0 && offset.value) await page(Math.max(0, offset.value - 25));
    return true;
  } catch (e) {
    actionError.value = describeError(e).message;
    return false;
  }
}
</script>
<template>
  <div class="page-heading">
    <h1>Model</h1>
    <RouterLink
      class="button primary"
      :to="{ path: '/models/new', query: { provider_id: provider || undefined } }"
      ><span aria-hidden="true">＋</span>创建 Model</RouterLink
    >
  </div>
  <div class="filters">
    <div class="field">
      <label for="provider-filter">按 Provider 筛选</label
      ><ProviderSelect id="provider-filter" v-model="provider" />
    </div>
  </div>
  <p v-if="actionError" class="error" role="alert">{{ actionError }}</p>
  <p v-if="pending" role="status">加载中…</p>
  <div v-else-if="error" role="alert">
    <p class="error">{{ error }}</p>
    <button @click="refresh">重试</button>
  </div>
  <template v-else-if="data">
    <p v-if="!data.items.length" class="panel">暂无匹配的 Model 配置。</p>
    <ul v-else class="records">
      <li
        v-for="item in data.items"
        :key="JSON.stringify([item.provider_id, item.model_name])"
        class="record"
      >
        <div>
          <h2>
            <RouterLink
              :to="{
                path: '/models/edit',
                query: { provider_id: item.provider_id, model_name: item.model_name },
              }"
              >{{ item.name }}</RouterLink
            >
          </h2>
          <p v-if="item.model_name !== item.name" class="muted">{{ item.model_name }}</p>
          <small>{{
            item.context_window === null
              ? "上下文容量未知"
              : `上下文 ${item.context_window.toLocaleString("en-US")} tokens`
          }}</small>
        </div>
        <ConfirmDelete :name="item.name" :remove="() => remove(item)"
          ><p v-if="actionError" class="error" role="alert">{{ actionError }}</p></ConfirmDelete
        >
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
