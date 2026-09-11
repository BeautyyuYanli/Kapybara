<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { listProviders, deleteProvider, discoverModels } from "@/api/generated";
import { useResource } from "@/composables/useResource";
import { describeError } from "@/api/errors";
import PageNavigation from "@/components/PageNavigation.vue";
import ConfirmDelete from "@/components/ConfirmDelete.vue";
import { presets } from "./presets";
const route = useRoute(),
  router = useRouter();
const offset = computed(() => Math.max(0, Number(route.query.offset) || 0));
const { data, pending, error, refresh } = useResource(
  () => route.fullPath,
  async (signal) =>
    (
      await listProviders({
        query: { offset: offset.value, limit: 25 },
        signal,
        throwOnError: true,
      })
    ).data,
);
const actionError = ref(""),
  discovery = ref(new Set<string>()),
  discovered = ref("");
function page(value: number) {
  return router.replace({ query: { offset: value || undefined } });
}
async function remove(id: string) {
  actionError.value = "";
  try {
    await deleteProvider({ path: { provider_id: id }, throwOnError: true });
    await refresh();
    if (data.value?.items.length === 0 && offset.value) await page(Math.max(0, offset.value - 25));
    return true;
  } catch (e) {
    actionError.value = describeError(e).message;
    return false;
  }
}
async function discover(id: string) {
  if (discovery.value.has(id)) return;
  discovery.value.add(id);
  actionError.value = "";
  discovered.value = "";
  try {
    await discoverModels({ path: { provider_id: id }, throwOnError: true });
    discovered.value = id;
  } catch (e) {
    actionError.value = describeError(e).message;
  } finally {
    discovery.value.delete(id);
  }
}
</script>
<template>
  <div class="page-heading">
    <div>
      <span class="eyebrow">01 / CONNECTIONS</span>
      <h1>Provider</h1>
      <p class="muted">管理模型服务的连接配置</p>
    </div>
    <RouterLink class="button primary" to="/providers/new">
      <span aria-hidden="true">＋</span>创建 Provider
    </RouterLink>
  </div>
  <p v-if="actionError" class="error" role="alert">{{ actionError }}</p>
  <p v-if="discovered" role="status">
    发现完成。<RouterLink :to="{ path: '/models', query: { provider_id: discovered } }"
      >查看模型目录</RouterLink
    >
  </p>
  <p v-if="pending" role="status">加载中…</p>
  <div v-else-if="error" role="alert">
    <p class="error">{{ error }}</p>
    <button @click="refresh">重试</button>
  </div>
  <template v-else-if="data">
    <div class="collection-heading">
      <span>服务连接</span><span>本页 {{ String(data.items.length).padStart(2, "0") }} 项</span>
    </div>
    <p v-if="!data.items.length" class="panel">暂无 Provider 配置。</p>
    <ul v-else class="records">
      <li v-for="(item, index) in data.items" :key="item.id" class="record">
        <span class="record-index" aria-hidden="true">
          {{ String(offset + index + 1).padStart(2, "0") }}
        </span>
        <div>
          <h2>
            <RouterLink :to="`/providers/${item.id}`">{{ item.name }}</RouterLink>
          </h2>
          <p class="muted">{{ item.base_url || "SDK 默认地址" }}</p>
          <small>{{
            presets.find((preset) => preset.model === item.model_class)?.label ?? item.model_class
          }}</small>
        </div>
        <div class="actions">
          <button :disabled="discovery.has(item.id)" @click="discover(item.id)">
            {{ discovery.has(item.id) ? "发现中…" : "发现模型" }}</button
          ><ConfirmDelete :name="item.name" provider :remove="() => remove(item.id)"
            ><p v-if="actionError" class="error" role="alert">{{ actionError }}</p></ConfirmDelete
          >
        </div>
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
