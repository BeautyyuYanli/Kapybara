<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";

const route = useRoute();
// List and form routes belong to the same navigation section.
const currentSection = computed(() => `/${route.path.split("/")[1]}`);
const navigation = [
  { to: "/providers", label: "Provider", caption: "01 / 连接" },
  { to: "/models", label: "Model", caption: "02 / 模型" },
  { to: "/sessions", label: "Session", caption: "03 / 会话" },
];
</script>
<template>
  <a class="skip" href="#main">跳至内容</a>
  <div class="app-frame">
    <header class="site-header">
      <RouterLink class="brand" to="/providers" aria-label="Kapy · 配置">
        k<span class="brand-dot">.</span>
      </RouterLink>
      <nav class="primary-nav" aria-label="主导航">
        <RouterLink
          v-for="item in navigation"
          :key="item.to"
          :to="item.to"
          :class="{ 'is-current': currentSection === item.to }"
          :aria-current="currentSection === item.to ? 'location' : undefined"
        >
          <span class="nav-index" aria-hidden="true">{{ item.caption }}</span>{{ item.label }}
        </RouterLink>
      </nav>
    </header>
    <main id="main" class="shell" tabindex="-1"><slot /></main>
  </div>
</template>
