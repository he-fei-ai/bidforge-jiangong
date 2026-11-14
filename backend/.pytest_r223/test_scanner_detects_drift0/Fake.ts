import api from "./index";
export const good = () => api.get("/ai/config");
export const bad = () => api.get("/definitely-not-a-real-endpoint");
