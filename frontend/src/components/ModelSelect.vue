<script setup lang="ts">
import { listModels } from "@/api/generated";
import { useOptions } from "@/composables/useOptions";
const value = defineModel<string>({ required: true });
const props = defineProps<{
  id: string;
  providerId: string;
  required?: boolean;
  describedby?: string;
  invalid?: boolean;
}>();
const { items, pending, hasMore, error, more } = useOptions(
  () => props.providerId,
  async (offset, signal) => {
    if (!props.providerId) return { items: [], has_more: false };
    return (
      await listModels({
        query: { provider_id: props.providerId, offset, limit: 25 },
        signal,
        throwOnError: true,
      })
    ).data;
  },
);
</script>
<template>
  <select
    :id="id"
    v-model="value"
    :required="required"
    :disabled="!providerId"
    :aria-describedby="describedby"
    :aria-invalid="invalid"
  >
    <option value="">{{ required ? "选择 Model" : "全部 Model" }}</option>
    <option v-if="value && !items.some((item) => item.model_name === value)" :value="value">
      {{ value }}（当前选择）
    </option>
    <option v-for="item in items" :key="item.model_name" :value="item.model_name">
      {{ item.name }} · {{ item.model_name }}
    </option>
  </select>
  <small v-if="error" role="alert" class="error">{{ error }}</small>
  <button
    v-if="hasMore || error || pending"
    type="button"
    :disabled="pending"
    @click="more()"
  >
    {{ pending ? "加载中…" : error ? "重试加载 Model" : "加载更多 Model" }}
  </button>
</template>
