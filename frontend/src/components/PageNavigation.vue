<script setup lang="ts">
defineProps<{ offset: number; count: number; hasMore: boolean }>();
defineEmits<{ change: [offset: number] }>();
</script>
<template>
  <nav class="actions" aria-label="分页">
    <button :disabled="offset === 0" @click="$emit('change', Math.max(0, offset - 25))">
      上一页
    </button>
    <span>本页 {{ count }} 条</span>
    <button :disabled="!hasMore" @click="$emit('change', offset + count)">下一页</button>
  </nav>
</template>
