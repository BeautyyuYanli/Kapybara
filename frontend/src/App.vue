<script setup lang="ts">
import AppShell from "./components/AppShell.vue";
</script>
<template>
  <AppShell
    ><RouterView v-slot="{ Component, route }"
      ><component :is="Component" :key="route.path + JSON.stringify(route.params)" /></RouterView
  ></AppShell>
</template>
