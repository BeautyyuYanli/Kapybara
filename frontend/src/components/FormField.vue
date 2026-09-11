<script setup lang="ts">
defineProps<{ id: string; label: string; help?: string; error?: string }>();
</script>
<template>
  <div class="field">
    <label :for="id">{{ label }}</label>
    <slot :id="id" :describedby="`${id}-help ${id}-error`" :invalid="!!error" />
    <small :id="`${id}-help`">{{ help }}</small>
    <small :id="`${id}-error`" class="error">{{ error }}</small>
  </div>
</template>
