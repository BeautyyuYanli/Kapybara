import { createApp } from "vue";
import App from "./App.vue";
import router from "./router";
import "./api/client";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/layout.css";
createApp(App).use(router).mount("#app");
