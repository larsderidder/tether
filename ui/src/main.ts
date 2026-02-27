import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import App from "./App.vue";
import "diff2html/bundles/css/diff2html.min.css";
import "./generated.css";
import ActiveSession from "./views/ActiveSession.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: ActiveSession },
    { path: "/session/:id", component: ActiveSession, name: "session" }
  ]
});

export { router };

createApp(App).use(router).mount("#app");
