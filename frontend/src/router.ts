import { createRouter, createWebHistory } from "vue-router";
export default createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: "/", redirect: "/providers" },
    { path: "/providers", component: () => import("./pages/providers/ProviderList.vue") },
    { path: "/providers/new", component: () => import("./pages/providers/ProviderForm.vue") },
    { path: "/providers/:id", component: () => import("./pages/providers/ProviderForm.vue") },
    { path: "/models", component: () => import("./pages/models/ModelList.vue") },
    { path: "/models/new", component: () => import("./pages/models/ModelForm.vue") },
    { path: "/models/edit", component: () => import("./pages/models/ModelForm.vue") },
    { path: "/sessions", component: () => import("./pages/sessions/SessionList.vue") },
    { path: "/sessions/new", component: () => import("./pages/sessions/SessionForm.vue") },
    { path: "/sessions/:id", component: () => import("./pages/sessions/SessionForm.vue") },
    { path: "/:pathMatch(.*)*", component: () => import("./pages/NotFound.vue") },
  ],
  scrollBehavior: () => ({ top: 0 }),
});
