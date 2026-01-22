import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import App from "./App.vue";
import "./generated.css";
import "diff2html/bundles/css/diff2html.min.css";
import ActiveSession from "./views/ActiveSession.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: "/", component: ActiveSession }]
});

createApp(App).use(router).mount("#app");
